import certifi
from urllib.request import urlopen
from pandas import DataFrame, Series, concat
from pandas import set_option as pandas_set_option
from random import randrange
from subprocess import Popen
from shutil import copyfileobj, copyfile
from threading import Event
import time
import pytz
import datetime
import json
import re
import os, sys
import dustmaker
import numpy as np
from dustkidtv.maps import STOCK_MAPS, CMP_MAPS, MAPS_WITH_THUMBNAIL, MAPS_WITH_ICON

TILE_WIDTH = 48
START_DELAY = 1112
DEATH_DELAY = 1000


def isDst(time, timezone="America/Los_Angeles"):
    timezone = pytz.timezone(timezone)
    localTime = timezone.localize(time, is_dst=None)
    return localTime.dst != datetime.timedelta(0, 0)


if isDst(datetime.datetime.utcnow()):
    CHANGE_DAILY_TIME = datetime.time(hour=4, tzinfo=datetime.timezone.utc)
else:
    CHANGE_DAILY_TIME = datetime.time(hour=5, tzinfo=datetime.timezone.utc)

pandas_set_option('display.max_rows', None)
pandas_set_option('display.max_columns', None)


def urlretrieve_with_cert(url: str, path: str, param: str = None):
    combined_url = f"{url}{param}" if param else f"{url}"
    with urlopen(combined_url, cafile=certifi.where()) as in_stream, open(path, 'wb') as out_file:
        copyfileobj(in_stream, out_file)


class InvalidReplay(Exception):
    pass


class ReplayQueue:
    maxHistoryLength = 50
    maxQueueLength = 100

    def findNewReplays(self, onlyValid=True):
        dustkidPage = urlopen("https://dustkid.com/", cafile=certifi.where())
        content = dustkidPage.read().decode(dustkidPage.headers.get_content_charset())

        markerStart = "init_replays = ["
        markerEnd = "];"
        start = content.find(markerStart) + len(markerStart)
        end = content[start:].find(markerEnd) + start

        replayListJson = "[" + content[start:end] + "]"

        replayList = json.loads(replayListJson)

        # converts this list of dicts to pandas dataframe
        replayFrame = DataFrame(replayList)

        if onlyValid:
            replayFrame.drop(replayFrame[replayFrame['validated'] != 1].index, inplace=True)

        return replayFrame

    def getBackupQueue(self, queueFilename='dustkidtv/assets/replays.json'):

        with open(queueFilename) as f:
            replayListJson = f.read()

        replayList = json.loads(replayListJson)
        replayFrame = DataFrame(replayList)
        return replayFrame

    def computeReplayWeight(self, rpl):
        try:
            # Fast replay good up to RANK_PRIORITY
            # this also priorities customs as a side effect
            factor = min([rpl['rank_all_score'], rpl['rank_all_time'], self.queuePriority['RANK_PRIORITY']])
            # PB good
            if rpl['pb']:
                factor /= self.queuePriority['PB_PRIORITY']
            # apples good
            if rpl['apples']:
                factor /= (self.queuePriority['APPLES_PRIORITY'] * rpl['apples'])
            # consite any% good
            if rpl['level'] == 'boxes' and rpl['time']<2000:
                factor /= self.queuePriority['CONSITE_PRIORITY']

        except TypeError:
            factor = 1

        weight = rpl['time'] * factor
        return weight

    def computeReplayPriority(self, metadata):
        weights = [self.computeReplayWeight(r) for _, r in metadata.iterrows()]
        return weights

    def sortReplays(self):
        self.queue["priority"] = self.computeReplayPriority(self.queue)
        self.queue.sort_values("priority", inplace=True, ignore_index=True)

    def getReplayId(self):
        ridList = self.queue['replay_id'].tolist()
        return ridList

    def updateHistory(self, id):
        self.history = self.history + [id]
        if len(self.history) > self.maxHistoryLength:
            self.history.pop(0)

    def updateQueue(self):
        newReplays = self.findNewReplays()

        # remove elements in history
        for id in self.history:
            newReplays.drop(newReplays[newReplays['replay_id'] == id].index, inplace=True)

        self.queue = concat([newReplays, self.queue], ignore_index=True)

        # remove elements already in queue
        self.queue.drop_duplicates(subset='replay_id', ignore_index=True, inplace=True)

        # remove old daily replays
        self.cleanDaily()

        queueLength = len(self.queue)
        if queueLength > self.maxQueueLength:
            self.queue = self.queue[:self.maxQueueLength]

        self.length = len(self.queue)
        self.sortReplays()
        self.queueId = self.getReplayId()

    def cleanDaily(self):
        today = datetime.datetime.utcnow().date()
        dailyTime = datetime.datetime.combine(today, CHANGE_DAILY_TIME).timestamp()

        # select old dailies replays
        dailys=[bool(re.fullmatch('random\d+', level)) for level in self.queue['level']]
        olds=(self.queue['timestamp'] < dailyTime)
        oldDaily = (olds) & (dailys)
        self.queue.drop(self.queue[oldDaily].index, inplace=True)

    def update(self, id):
        self.updateHistory(id)
        self.updateQueue()

    def next(self):
        if self.length > 0:
            self.current = Replay(self.queue.iloc[0], debug=self.debug)

            self.queue.drop(self.queue.index[0], inplace=True)

            self.queueId.pop(0)
        else:
            # select random replay from backup queue
            randId = randrange(len(self.backupQueue))
            self.current = Replay(self.backupQueue.iloc[randId], debug=self.debug)
            self.backupCounter += 1

        self.counter += 1

        if self.debug:
            with open('dustkidtv.log', 'a', encoding='utf-8') as logfile:
                logfile.write('\n')
                logfile.write('--------------------------------------------------------------------------------\n')
                logfile.write('\n')
                logfile.write('Replays played: %5i\t Replays in queue: %3i\n' % (self.counter, self.length))
                logfile.write('New rep played: %5i\t Old rep played: %5i\n' % (
                    self.counter - self.backupCounter, self.backupCounter))

                if self.debug > 1:
                    logfile.write('\nQueue:\n')
                    logfile.write(str(self.queue))
                    logfile.write('\n')
                    logfile.write('\nHistory:\n')
                    logfile.write(str(self.history) + '\n')
                    logfile.write('\n')

                logfile.write('Current replay: %i\t Timestamp: %s UTC\n' % (
                    self.current.replayId, time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime(self.current.timestamp))))
                logfile.write('Level: %s\t Player: %s\t Time: %.3f s\n' % (
                    self.current.level, self.current.username, self.current.time / 1000.))
                logfile.write('\n')

        return (self.current)

    def __init__(self, debug=False, priority=None):
        self.debug = debug
        self.queuePriority = priority
        self.history = []
        self.counter = 0
        self.queue = self.findNewReplays()
        self.length = len(self.queue)
        self.sortReplays()
        self.queueId = self.getReplayId()
        self.current = None
        self.backupQueue = self.getBackupQueue()
        self.backupCounter = 0


class Replay:

    def openReplay(self, url):
        try:
            if sys.platform == 'win32':
                if not (url.startswith('http://') or url.startswith('https://')):
                    url = url.replace('/', '\\')
                os.startfile(url)
            elif sys.platform == 'darwin':
                Popen(['open', url])
            else:
                Popen(['xdg-open', url])

        except OSError:
            print('Can\'t open dustforce URI: ' + url)
            if self.debug:
                with open('dustkidtv.log', 'a', encoding='utf-8') as logfile:
                    logfile.write('Error: Can\'t open dustforce URI: ' + url + '\n')
            raise

    def downloadReplay(self):
        path = 'dfreplays/' + str(self.replayId) + '.dfreplay'
        if os.path.isfile(path):
            return path
        urlretrieve_with_cert("https://dustkid.com/backend8/get_replay.php?replay=", path, str(self.replayId))
        return path

    def getReplayUri(self):
        return "dustforce://replay/" + str(self.replayId)

    def getReplayJson(self):
        return f"https://dustkid.com/replayviewer.php?replay_id={str(self.replayId)}" + "&json=true&metaonly"

    def getReplayPage(self):
        return "https://dustkid.com/replay/" + str(self.replayId)

    def loadMetadataFromJson(self, replayJson):
        metadata = json.loads(replayJson)
        return metadata

    def loadMetadataFromPage(self, id):
        replayPage = urlopen(self.getReplayJson())
        content = replayPage.read().decode(replayPage.headers.get_content_charset())

        if 'Could not find replay' in content:
            raise InvalidReplay

        else:
            metadata = json.loads(content)
            return metadata

    def getReplayFrames(self):
        with dustmaker.DFReader(open(self.replayPath, "rb")) as reader:
            replay = reader.read_replay()

        entity_data = replay.get_player_entity_data()
        if entity_data is None:
            replayFrames = None

        else:
            nframes = len(entity_data.frames)
            replayFrames = np.empty([nframes, 5])
            i = 0
            for frame in entity_data.frames:
                replayFrames[i] = [frame.frame, frame.x_pos, frame.y_pos, frame.x_speed, frame.y_speed]
                i += 1

        return replayFrames

    def estimateDeaths(self):

        def doBBoxDistance(point, box):
            x, y = point
            x1, y1, x2, y2 = box

            inXRange = (x >= x1 and x <= x2)
            inYRange = (y >= y1 and y <= y2)

            dx = np.minimum(np.abs(x - x1), np.abs(x - x2))
            dy = np.minimum(np.abs(y - y1), np.abs(y - y2))

            if inXRange and inYRange:
                d = 0.
            elif inXRange:
                d = dy
            elif inYRange:
                d = dx
            else:
                d = np.sqrt(dx * dx + dy * dy)
            return d

        def compareToCheckpoints(candidates, coords, checkpoints, kTiles=4):
            estimatedDeathIdx = []
            for idx in candidates:
                coord = coords[idx]
                for cp in checkpoints:
                    distance = np.sqrt(np.sum((coord - cp) ** 2))
                    if distance < (kTiles * TILE_WIDTH):
                        estimatedDeathIdx.append(idx)
                        break
            return np.array(estimatedDeathIdx)

        def getCandidates(err, kTiles=2):
            candidates = np.where(err > kTiles * TILE_WIDTH)[0]
            return candidates

        replayFrames = self.getReplayFrames()
        if replayFrames is None or replayFrames.shape[0] < 2:
            if self.debug:
                with open('dustkidtv.log', 'a', encoding='utf-8') as logfile:
                    logfile.write("Warning: not enough desync data to estimate deaths\n")
            return 0

        if replayFrames.shape[1] != 5:
            raise ValueError('Unexpected data in replay frames')

        nframes = len(replayFrames)

        frames = replayFrames[:, 0]
        lastFrame = int(frames[-1])

        t = frames / 50.
        coords = replayFrames[:, [1, 2]]
        velocity = replayFrames[:, [3, 4]]

        checkpoints = self.levelFile.getCheckpointsCoordinates()
        ncheckpoints = len(checkpoints)

        deltat = t[1:] - t[:-1]

        estimatedCoords = np.empty((nframes, 2))
        estimatedCoords[0] = coords[0]
        estimatedCoords[1:] = coords[:-1] + velocity[:-1] * (deltat).reshape((nframes - 1, 1))

        estimatedCoords2 = np.empty((nframes, 2))
        estimatedCoords2[0] = coords[0]
        estimatedCoords2[1:] = coords[:-1] + velocity[1:] * (deltat).reshape((nframes - 1, 1))

        estimatedBox = np.c_[
            np.minimum(estimatedCoords[:, 0], estimatedCoords2[:, 0]),
            np.minimum(estimatedCoords[:, 1], estimatedCoords2[:, 1]),
            np.maximum(estimatedCoords[:, 0], estimatedCoords2[:, 0]),
            np.maximum(estimatedCoords[:, 1], estimatedCoords2[:,1])
        ]  # box boundary defined as [[x1, y1, x2, y2]]

        err = np.zeros(nframes, dtype=float)
        for frame in range(nframes):
            point = coords[frame]
            box = estimatedBox[frame]
            d = doBBoxDistance(point, box)
            err[frame] = d

        candidates = getCandidates(err)
        estimatedDeathIdx = compareToCheckpoints(candidates, coords, checkpoints)
        ndeaths = len(estimatedDeathIdx)

        return ndeaths

    def saveInfoToFile(self):
        out = '%s %s %s in %.3fs' % (self.levelname, self.completion, self.finesse, self.username, rep.time / 1000.)
        with open('replayinfo.txt', 'w') as f:
            f.write(out)

    def __init__(self, metadata=None, replayId=None, replayJson=None, debug=False):

        self.debug = debug

        if metadata is not None:
            self.replayId = metadata['replay_id']
        elif replayId is not None:
            self.replayId = replayId
            metadata = self.loadMetadataFromPage(replayId)
        elif replayJson is not None:
            metadata = self.loadMetadataFromJson(replayJson)
            self.replayId = metadata['replay_id']
        else:
            if self.debug:
                with open('dustkidtv.log', 'a', encoding='utf-8') as logfile:
                    logfile.write('Error: No replay provided\n')
            raise ValueError('No replay provided')

        self.validated = metadata['validated']

        self.time = metadata['time']

        # download replay from dustkid
        self.replayPath = self.downloadReplay()

        self.numplayers = metadata['numplayers']

        self.characterNum = metadata['character']
        characters = ["Dustman", "Dustgirl", "Dustkid", "Dustworth"]

        self.completionNum = metadata['score_completion']
        self.finesseNum = metadata['score_finesse']
        scores = ['D', 'C', 'B', 'A', 'S']
        self.completion = scores[self.completionNum - 1]
        self.finesse = scores[self.finesseNum - 1]

        self.apple = metadata['apples']
        self.isPB = metadata['pb']

        self.timestamp = metadata['timestamp']
        self.username = metadata['username']
        self.levelname = metadata['levelname']  # public level name
        self.level = metadata['level']  # in game level name

        print('\nopening replay %i of %s (%.3f s)' % (self.replayId, self.level, self.time / 1000.))

        self.levelFile = Level(self.level, debug=self.debug)
        if self.levelFile.hasThumbnail:
            self.thumbnail = self.levelFile.getThumbnail()
        else:
            self.thumbnail = None

        # estimation of replay length in real time
        if self.numplayers > 1 or not self.levelFile.levelPath:  # can't estimate deaths on dustkid daily
            self.deaths = 0
        else:
            self.deaths = self.estimateDeaths()
        self.realTime = (self.time + START_DELAY + self.deaths * DEATH_DELAY) / 1000.

        self.skip = Event()


class Level:

    def downloadLevel(self):
        path = 'dflevels/' + str(self.name)
        if os.path.isfile(path):
            return path
        id = re.match('\d+', self.name[::-1]).group()[::-1]

        print('Downloading ' + "http://atlas.dustforce.com/gi/downloader.php?id=%s" % id)
        if self.debug:
            with open('dustkidtv.log', 'a', encoding='utf-8') as logfile:
                logfile.write('Downloading ' + "http://atlas.dustforce.com/gi/downloader.php?id=%s\n" % id)

        urlretrieve_with_cert("http://atlas.dustforce.com/gi/downloader.php?id=", path, id)

        return path

    def downloadDaily(self):
        path = 'dflevels/' + str(self.name)  # this is in the format random-dddd
        if os.path.isfile(path) and self.dailyIsCurrent:
            return path

        print('Downloading ' + "https://dustkid.com/backend8/level.php?id=random")
        if self.debug:
            with open('dustkidtv.log', 'a', encoding='utf-8') as logfile:
                logfile.write('Downloading ' + "https://dustkid.com/backend8/level.php?id=random\n")

        urlretrieve_with_cert("https://dustkid.com/backend8/level.php?id=random", path)

        copyfile(path, self.levelPath)  # daily name in df folder is always random (no counter appended)
        self.dailyIsCurrent = True

        return path

    def getCheckpointsCoordinates(self):
        checkpoints = []

        with dustmaker.DFReader(open(self.levelPath, "rb")) as reader:
            levelFile = reader.read_level()
            entities = levelFile.entities

            for entity in entities.values():
                if isinstance(entity[2], dustmaker.entity.CheckPoint):
                    checkpoints.append([entity[0], entity[1]])

        return np.array(checkpoints)

    def getThumbnail(self):

        if self.isStock:
            if self.hasLevelIcon:
                imgPath = 'dustkidtv/assets/icons/%s.png' % self.name
                with open(imgPath, 'rb') as f:
                    thumbnail = f.read()
                return thumbnail

        with dustmaker.DFReader(open(self.levelPath, "rb")) as reader:
            level = reader.read_level()
            thumbnail = level.sshot

        return thumbnail

    def __init__(self, level, debug=False):
        self.debug = debug

        try:
            self.dfPath = os.environ['DFPATH']
            self.dfDailyPath = os.environ['DFDAILYPATH']

        except KeyError:
            with open(configFile, 'r') as f:
                conf = json.load(f)
                self.dfPath = conf['path']
                self.dfDailyPath = conf['user_path']

        self.name = level

        self.isStock = level in STOCK_MAPS
        self.isCmp = level in CMP_MAPS
        self.isInfini = level == 'exec func ruin user'
        self.isDaily = re.fullmatch('random\d+', level)

        if self.isStock:
            self.levelPath = self.dfPath + "/content/levels2/" + level
            self.hasLevelThumbnail = (level in MAPS_WITH_THUMBNAIL)
            self.hasLevelIcon = (level in MAPS_WITH_ICON)
            self.hasThumbnail = self.hasLevelThumbnail or self.hasLevelIcon
        elif self.isCmp:
            self.levelPath = self.dfPath + "/content/levels3/" + level
            self.hasThumbnail = True
        elif self.isInfini:
            self.levelPath = 'dustkidtv/assets/infinidifficult_fixed'
            self.hasThumbnail = False
        elif self.isDaily:
            self.levelPath = self.dfDailyPath + "/user/levels/random"
            dailyPath = 'dflevels/' + str(self.name)
            self.hasThumbnail = True
            self.dailyIsCurrent = os.path.isfile(dailyPath)
            self.downloadDaily()
        else:
            self.levelPath = self.downloadLevel()
            self.hasThumbnail = True
