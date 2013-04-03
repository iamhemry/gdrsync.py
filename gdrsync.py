#!/usr/bin/python

import config

import logging

logging.basicConfig()
logging.getLogger().setLevel(config.PARSER.get('gdrsync', 'logLevel'))

import apiclient.http
import driveutils
import localfolder
import mimetypes
import remotefolder
import requestexecutor
import sys
import time

MIB = 0x100000
CHUNKSIZE = 1 * MIB

KIB = float(0x400)
PERCENTAGE = 100.0

DEFAULT_MIME_TYPE = 'application/octet-stream'

LOGGER = logging.getLogger(__name__)

class GDRsync(object):
    def __init__(self):
        self.localFolderFactory = localfolder.Factory()
        self.remoteFolderFactory = remotefolder.Factory()

    def sync(self, localPath, remotePath):
        LOGGER.info('Starting...')

        self._sync(self.localFolderFactory.create(localPath),
                self.remoteFolderFactory.create(remotePath))

        LOGGER.info('End.')

    def _sync(self, localFolder, remoteFolder):
        remoteFolder = self.trash(localFolder, remoteFolder)

        remoteFolder = self.insertFolders(localFolder, remoteFolder)
        remoteFolder = self.copyFiles(localFolder, remoteFolder)

        for localFile in localFolder.folders():
            remoteFile = remoteFolder.children[localFile.name]

            childLocalFolder = self.localFolderFactory.create(localFile)
            childRemoteFolder = self.remoteFolderFactory.create(remoteFile)

            self._sync(childLocalFolder, childRemoteFolder)

    def trash(self, localFolder, remoteFolder):
        remoteFolder = self.trashDuplicate(localFolder, remoteFolder)
        remoteFolder = self.trashExtraneous(localFolder, remoteFolder)
        remoteFolder = self.trashDifferentType(localFolder, remoteFolder)

        return remoteFolder

    def trashDuplicate(self, localFolder, remoteFolder):
        for remoteFile in remoteFolder.duplicate:
            LOGGER.debug('%s: Duplicate file.', remoteFile.path)

            remoteFile = self.trashFile(remoteFile)

        return remoteFolder.withoutDuplicate()

    def trashFile(self, remoteFile):
        LOGGER.info('%s: Trashing file...', remoteFile.path)

        def request():
            return (driveutils.DRIVE.files()
                    .trash(fileId = remoteFile.delegate['id'],
                            fields = driveutils.FIELDS)
                    .execute())

        file = requestexecutor.execute(request)

        return remoteFile.withDelegate(file)

    def trashExtraneous(self, localFolder, remoteFolder):
        output = remotefolder.RemoteFolder(remoteFolder.file)
        for remoteFile in remoteFolder.children.values():
            if remoteFile.name in localFolder.children:
                output.addChild(remoteFile)
                continue

            LOGGER.debug('%s: Extraneous file.', remoteFile.path)

            remoteFile = self.trashFile(remoteFile)

        return output

    def trashDifferentType(self, localFolder, remoteFolder):
        output = remotefolder.RemoteFolder(remoteFolder.file)
        for remoteFile in remoteFolder.children.values():
            localFile = localFolder.children[remoteFile.name]
            if localFile.folder == remoteFile.folder:
                output.addChild(remoteFile)
                continue

            LOGGER.debug('%s: Different type.', remoteFile.path)

            remoteFile = self.trashFile(remoteFile)

        return output

    def insertFolders(self, localFolder, remoteFolder):
        output = (remotefolder.RemoteFolder(remoteFolder.file)
                .addChildren(remoteFolder.children.values()))
        for localFile in localFolder.folders():
            remoteFile = remoteFolder.children.get(localFile.name)
            if remoteFile is not None:
                LOGGER.debug('%s: Existent folder.', remoteFile.path)
                continue

            remoteFile = remoteFolder.createRemoteFile(localFile.name, 
                    driveutils.MIME_FOLDER)
            remoteFile = self.insertFolder(localFile, remoteFile)

            output.addChild(remoteFile)

        return output

    def insertFolder(self, localFile, remoteFile):
        LOGGER.info('%s: Inserting folder...', remoteFile.path)

        def request():
            return (driveutils.DRIVE.files().insert(body = remoteFile.delegate,
                    fields = driveutils.FIELDS).execute())

        file = requestexecutor.execute(request)

        return remoteFile.withDelegate(file)

    def copyFiles(self, localFolder, remoteFolder):
        output = (remotefolder.RemoteFolder(remoteFolder.file)
                .addChildren(remoteFolder.children.values()))
        for localFile in localFolder.files():
            remoteFile = remoteFolder.children.get(localFile.name)

            fileOperation = self.fileOperation(localFile, remoteFile)
            if fileOperation is None:
                continue

            if remoteFile is None:
                remoteFile = remoteFolder.createRemoteFile(localFile.name)
            remoteFile = fileOperation(localFile, remoteFile)

            output.addChild(remoteFile)

        return output

    def fileOperation(self, localFile, remoteFile):
        if remoteFile is None:
            return self.insert
        if remoteFile.size != localFile.size:
            LOGGER.debug('%s: Different size.', remoteFile.path)

            return self.update
        if remoteFile.modified != localFile.modified:
            if remoteFile.md5 != localFile.md5:
                LOGGER.debug('%s: Different checksum.', remoteFile.path)

                return self.update

            return self.touch

        LOGGER.debug('%s: Up to date.', remoteFile.path)

        return None

    def insert(self, localFile, remoteFile):
        LOGGER.info('%s: Inserting file...', remoteFile.path)

        body = remoteFile.delegate.copy()
        body['modifiedDate'] = driveutils.formatTime(localFile.modified)

        (mimeType, encoding) = mimetypes.guess_type(localFile.delegate)
        if mimeType is None:
            mimeType = DEFAULT_MIME_TYPE

        media = apiclient.http.MediaFileUpload(localFile.delegate,
                mimetype = mimeType, chunksize = CHUNKSIZE, resumable = True)

        def request():
            request = (driveutils.DRIVE.files().insert(body = body,
                    media_body = media, fields = driveutils.FIELDS))

            start = time.time()
            while True:
                (progress, file) = request.next_chunk()
                if file is not None:
                    self.logProgress(remoteFile.path, start, localFile.size)

                    return file

                self.logProgress(remoteFile.path, start,
                        progress.resumable_progress, progress.total_size,
                        progress.progress())

        file = requestexecutor.execute(request)

        return remoteFile.withDelegate(file)

    def logProgress(self, path, start, bytesUploaded, bytesTotal = None,
            progress = 1.0):
        if bytesTotal is None:
            bytesTotal = bytesUploaded

        elapsed = time.time() - start

        kiB = round(bytesUploaded / KIB)
        progressPercentage = round(progress * PERCENTAGE)
        s = round(elapsed)

        kiBs = self.kiBs(bytesUploaded, elapsed)
        eta = self.eta(elapsed, bytesUploaded, bytesTotal)

        LOGGER.info('%s: %d%% (%dKiB / %ds = %dKiB/s) ETA: %ds', path,
                progressPercentage, kiB, s, kiBs, eta)

    def kiBs(self, bytesUploaded, elapsed):
        if round(elapsed) == 0:
            return 0

        return round((bytesUploaded / KIB) / elapsed)

    def eta(self, elapsed, bytesUploaded, bytesTotal):
        if bytesUploaded == 0:
            return 0
        
        bS = bytesUploaded / elapsed
        finish = bytesTotal / bS

        return round(finish - elapsed)

    def update(self, localFile, remoteFile):
        LOGGER.info('%s: Updating file...', remoteFile.path)

        body = remoteFile.delegate.copy()
        body['modifiedDate'] = driveutils.formatTime(localFile.modified)

        (mimeType, encoding) = mimetypes.guess_type(localFile.delegate)
        if mimeType is None:
            mimeType = DEFAULT_MIME_TYPE

        media = apiclient.http.MediaFileUpload(localFile.delegate,
                mimetype = mimeType, chunksize = CHUNKSIZE, resumable = True)

        def request():
            request = (driveutils.DRIVE.files()
                    .update(fileId = remoteFile.delegate['id'], body = body,
                            media_body = media, setModifiedDate = True,
                            fields = driveutils.FIELDS))

            start = time.time()
            while True:
                (progress, file) = request.next_chunk()
                if file is not None:
                    self.logProgress(remoteFile.path, start, localFile.size)

                    return file

                self.logProgress(remoteFile.path, start,
                        progress.resumable_progress, progress.total_size,
                        progress.progress())

        file = requestexecutor.execute(request)

        return remoteFile.withDelegate(file)

    def touch(self, localFile, remoteFile):
        LOGGER.debug('%s: Updating modified date...', remoteFile.path)

        body = {'modifiedDate': driveutils.formatTime(localFile.modified)}

        def request():
            return (driveutils.DRIVE.files()
                    .patch(fileId = remoteFile.delegate['id'], body = body,
                            setModifiedDate = True, fields = driveutils.FIELDS)
                    .execute())

        file = requestexecutor.execute(request)

        return remoteFile.withDelegate(file)

GDRsync().sync(sys.argv[1], sys.argv[2])
