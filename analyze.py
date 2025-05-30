import math
import threading
import time

import pandas as pd
import yaml
from scipy.spatial import ConvexHull
from sklearn.cluster import DBSCAN

from frameScanner import frameScanner
from mavlinkManager import mavlinkManager
from utils import RunMode


class analyzer:
    def __init__(self, timestamp, mode, videoStream):
        with open("config.yaml", "r") as f:
            self.config = yaml.safe_load(f)

        self.mode = mode
        self.positions = pd.DataFrame({'id': [], 'lat': [], 'lon': [], 'alt': [], 'time': [], 'color': [], 'type': []})
        self.positionsLong = pd.DataFrame(
            {'id': [], 'lat': [], 'lon': [], 'alt': [], 'time': [], 'color': [], 'type': []}
        )
        self.hullSets = []
        self.stopSignal = False

        self.fsInterface = frameScanner(videoStream, mode, timestamp)

        videoDuration = 0.0
        if mode is RunMode.RECORDED:
            videoDuration = self.fsInterface.duration

        self.mavlink = mavlinkManager(mode, timestamp, videoDuration)

        print("Run mode is: ", mode.name)

        self.analyzeThread = threading.Thread(target=self.analyzeLoop)
        self.analyzeThread.start()

    def shutdown(self):
        self.stopSignal = True
        self.analyzeThread.join()

    def updatePositions(self, row):
        if row['id'] in self.positions['id'].values:
            self.positions.loc[self.positions['id'] == row['id'], ['lat', 'lon', 'alt', 'time']] = (
                row['lat'],
                row['lon'],
                row['alt'],
                row['time'],
            )
        else:
            self.positions = pd.concat([self.positions, pd.DataFrame([row])], ignore_index=True)

        if row['id'] in self.positionsLong['id'].values:
            self.positionsLong.loc[self.positionsLong['id'] == row['id'], ['lat', 'lon', 'alt', 'time']] = (
                row['lat'],
                row['lon'],
                row['alt'],
                row['time'],
            )
        else:
            self.positionsLong = pd.concat([self.positionsLong, pd.DataFrame([row])], ignore_index=True)

    def computeHulls(self):
        self.hullSets = []
        for type in self.config['analyze']['detections']:
            filter = self.positions[self.positions['type'] == type]

            points = []
            for _, row in filter.iterrows():
                points.append((row['lon'], row['lat']))

            # Need several points in a class to do anything meaningful
            if len(points) > 3:
                db = DBSCAN(eps=self.config['groups']['epsilon'], min_samples=self.config['groups']['minSamples']).fit(
                    points
                )

                # ML Density Based grouping
                groupedPoints = {}
                for point, label in zip(points, db.labels_):
                    if label == -1:
                        continue
                    if label not in groupedPoints:
                        groupedPoints[label] = []  # Create a new list for this label if not already exists
                    groupedPoints[label].append(point)

                # Find hull for each group of points
                for label, subset in groupedPoints.items():
                    hull = ConvexHull(subset)
                    hullLines = []
                    for simplex in hull.simplices:
                        hullLines.append([subset[simplex[0]], subset[simplex[1]]])
                    self.hullSets.append(hullLines)

    def analyzeLoop(self):
        dataTimeout = 0
        hasStartedRecord = False
        while dataTimeout < self.config['analyze']['waitTime'] and not self.stopSignal:
            # Get camera data
            ret, frame, fwidth, fheight = self.fsInterface.getFrame()

            # Where are we?
            geoMsg = self.mavlink.getGEO()
            attMsg = self.mavlink.getATT()

            if not ret or geoMsg is None or attMsg is None:
                print("No data in either frames or mav data!")
                dataTimeout += 1
                time.sleep(1)
                continue

            if not hasStartedRecord and self.mode == RunMode.LIVE:
                self.mavlink.readyToRecord = True
                self.fsInterface.readyToRecord = True
                self.fsInterface.startTime = time.time()
                print(f"Started recording at: {time.time()}")
                hasStartedRecord = True

            # frame = self.fsInterface.rotateFrame(frame, attMsg['roll'])

            if self.config['analyze']['doDetections']:
                trimX1 = self.config['camera']['trimX1']
                trimX2 = self.config['camera']['trimX2']
                trimY1 = self.config['camera']['trimY1']
                trimY2 = self.config['camera']['trimY2']

                frame = frame[trimY1 : fheight - trimY2, trimX1 : fwidth - trimX2]
                frame, results = self.fsInterface.getIdentifiedFrame(frame)
                detectionData = results[0].summary()

                altitude = geoMsg["relative_alt"] / 1000
                planeLat = geoMsg["lat"] / 10000000
                planeLon = geoMsg["lon"] / 10000000
                planeHeading = geoMsg['hdg'] / 100 - self.config['analyze']['gpsMount']
                planeTilt = attMsg['pitch']

                # Remove detections older than 0.5 sec and update plane coords
                self.positions = self.positions[
                    self.positions['time'] > time.time() - self.config['analyze']['dataTimeout']
                ]
                self.positionsLong = self.positionsLong[self.positionsLong['time'] > time.time() - 10.0]
                planeUpdate = {
                    "id": "Plane",
                    "lat": planeLat,
                    "lon": planeLon,
                    "alt": altitude,
                    "time": time.time(),
                    'color': 'green',
                }
                self.updatePositions(planeUpdate)

                stableSpeed = 0.8
                rollSpeed = abs(attMsg['rollspeed'])
                pitchSpeed = abs(attMsg['pitchspeed'])
                yawSpeed = abs(attMsg['yawspeed'])

                # check if twisting slow enough for good data, set to 0.0 for all data
                if rollSpeed < stableSpeed and pitchSpeed < stableSpeed and yawSpeed < stableSpeed:
                    # Camera info
                    cameraSensorW = self.config['camera']['cameraSensorW']
                    cameraSensorH = self.config['camera']['cameraSensorH']
                    cameraPixelsize = self.config['camera']['cameraPixelsize']
                    cameraFocalLength = self.config['camera']['cameraFocalLength']
                    cameraTilt = self.config['camera']['cameraTilt'] * (math.pi / 180)

                    totalTilt = cameraTilt + planeTilt

                    # Basic Ground sample distance, how far in M each pixel is
                    nadirGSDH = (altitude * cameraSensorH) / (cameraFocalLength * self.config['camera']['height'])
                    nadirGSDW = (altitude * cameraSensorW) / (cameraFocalLength * self.config['camera']['width'])

                    cameraCenterX = fwidth / 2
                    cameraCenterY = fheight / 2

                    for i, detection in enumerate(detectionData):
                        # Camera is at a tilt from the ground, so GSD needs to be scaled
                        # by relative distance. Assuming camera is level horizontally, so
                        # just need to scale tilt in camera Y direction
                        if detection["name"] in self.config['analyze']['detections']:
                            box = detection["box"]
                            objectX = ((box["x2"] - box["x1"]) / 2) + box["x1"] + trimX1
                            objectY = ((box["y2"] - box["y1"]) / 2) + box["y1"] + trimY1

                            tanPhi = cameraPixelsize * ((objectY - cameraCenterY) / cameraFocalLength)
                            verticalPhi = math.atan(tanPhi)

                            totalAngle = totalTilt - verticalPhi

                            # sanity check if past 90 degrees
                            if totalAngle > self.config['analyze']['maxAngle']:
                                totalAngle = self.config['analyze']['maxAngle']

                            adjustedGSDH = nadirGSDH * (1 / math.cos(totalAngle))
                            adjustedGSDW = nadirGSDW * (1 / math.cos(totalAngle))

                            # Distance camera center is projected forward
                            offsetCenterY = math.tan(totalTilt) * altitude

                            # Positive value means shift left from camera POV
                            offsetYInPlaneFrame = (cameraCenterX - objectX) * adjustedGSDW

                            # Positive value means shift up in camera POV
                            offsetXInPlaneFrame = ((cameraCenterY - objectY) * adjustedGSDH) + offsetCenterY

                            if self.config['analyze']['cleanData']:
                                # exclude values that are too far away as noise
                                if abs(offsetXInPlaneFrame) > self.config['analyze']['maxDistance']:
                                    continue

                                # exclude values when plane too low
                                if altitude < self.config['analyze']['minHeight']:
                                    continue

                            # north is hdg value of 0/360, convert to normal radians with positive
                            # being counter clockwise
                            rotation = (90 - planeHeading) * (math.pi / 180)

                            # Plane is rotated around world frame by heading, so rotate camera detection back
                            worldXinMeters = offsetXInPlaneFrame * math.cos(rotation) - offsetYInPlaneFrame * math.sin(
                                rotation
                            )
                            worldYinMeters = offsetXInPlaneFrame * math.sin(rotation) + offsetYInPlaneFrame * math.cos(
                                rotation
                            )

                            # Simple meters to lat/lon, can be improved. 1 degree is about 111111 meters
                            objectLon = planeLon + (worldXinMeters * (1 / 111111.0))
                            objectLat = planeLat + (worldYinMeters * (1 / 111111.0))

                            # update
                            if 'track_id' in detection:
                                name = detection['name'] + str(detection['track_id'])
                            else:
                                name = detection['name'] + str(i)

                            for i, type in enumerate(self.config['analyze']['detections']):
                                if detection['name'] == type:
                                    color = self.config['analyze']['scatterColors'][i]

                            detectionUpdate = {
                                "id": name,
                                "lat": objectLat,
                                "lon": objectLon,
                                "alt": 0.0,
                                "time": time.time(),
                                "color": color,
                                "type": detection['name'],
                            }
                            self.updatePositions(detectionUpdate)

                    # after all detections are done in a frame cycle, compute hulls for groups
                    self.computeHulls()

            self.fsInterface.showFrame(frame)
            dataTimeout = 0

        print("closing analyze loop")
