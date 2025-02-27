from flask import request, Response, make_response, current_app, send_file
from flask_jwt_extended import jwt_required, get_jwt_identity, get_jti, decode_token
from flask_restful import Resource

from shared.factories import db, fs, seedr
from shared.utils import json_utils as JU
from models.torrents import Torrent
from models.users import User
from models.public_urls import PublicURLS

import time
import os
import re
import base64
import hashlib
import mimetypes
import shutil
import mmap
import contextlib

class PublicUrl(Resource):
    @jwt_required
    def post(self, *args, **kwargs):
        # create public url from file path
        file_path = JU.extract_keys(request.get_json(), "file_path")
        if not file_path: return JU.make_response("invalid data", 400)

        username = get_jwt_identity()
        user = User.find_by_username(username)
        if not user: return JU.make_response(f"user '{username}' doesn't exists", 404)

        if os.path.exists(file_path):
            record = PublicURLS.find_by_file_path(file_path=file_path)
            if record: return record.as_dict()

            created_at = int(time.time())
            
            file_path_hash = hashlib.sha1(str(file_path).encode()).hexdigest()[:10]
            expire_after = int(current_app.config.get('PUBLIC_URL_EXPIRES').total_seconds())
            record = PublicURLS(file_path=file_path, public_url_hash=file_path_hash, username=username, created_at=created_at, expire_after=expire_after)
            record.save_to_db()
            return record.as_dict()
        else:
            return JU.make_response(f"file '{file_path}' doesn't exists", 404)
    
    def get(self, public_url_hash):
        access_token = request.args.get('token', None)

        if not access_token:
            query = PublicURLS.find_by_public_url_hash(public_url_hash=public_url_hash)
            if not query: return JU.make_response(f"url not found", 404)
            if not query.is_valid: return JU.make_response(f"url expired", 410)
            return self.send_file(query.file_path, request.args.get('download', None))

        else:
            access_token = decode_token(access_token)
            if time.time() > access_token.get('exp'): JU.make_response("token expired", 401)

            try:
                path = base64.b64decode(public_url_hash).decode('utf-8')
            except:
                return JU.make_response("invalid path", 400)

            return self.send_file(path)
    
    def send_file(self, path, download=False):
        if not os.path.exists(path):
            return JU.make_response("invalid path", 400)
            
        elif os.path.isdir(path):
            return JU.make_response("downloading directories not allowed", 400)

        elif os.path.exists(path):
            if download:
                # send raw file that can be downloaded directly
                return send_file(path, mimetype='application/octet-stream', attachment_filename=os.path.basename(path), as_attachment=True)
            
            # stream file over byte range header
            range_header = request.headers.get('Range', None)
            byte1, byte2 = 0, None
            if range_header:
                match = re.search(r'(\d+)-(\d*)', range_header)
                groups = match.groups()

                if groups[0]:
                    byte1 = int(groups[0])
                if groups[1]:
                    byte2 = int(groups[1])

            try:
                mimetype = mimetypes.guess_type(path)[0]
                if mimetype is None:
                    mimetype = 'text/plain'
                elif mimetype.startswith('video'):
                    mimetype = 'video/mp4'
            except:
                return self.send_file(path, download=True)

            chunk, start, length, file_size = self.get_chunk(path, byte1, byte2)
            resp = Response(chunk, 206, mimetype=mimetype,
                            content_type=mimetype, direct_passthrough=True)
            resp.headers.add('Content-Range', 'bytes {0}-{1}/{2}'.format(start, start + length - 1, file_size))
            return resp

    def get_chunk(self, full_path, byte1=None, byte2=None):
        file_size = os.stat(full_path).st_size
        start = 0
        length = 1024

        if byte1 < file_size:
            start = byte1
        if byte2:
            length = byte2 + 1 - byte1
        else:
            length = file_size - start

        with open(full_path, 'rb') as f:
            with contextlib.closing(mmap.mmap(f.fileno(), length=0, access=mmap.ACCESS_READ)) as m:
                m.seek(start)
                chunk = m.read(length)
                m.flush()

        return chunk, start, length, file_size
    
class CopyFile(Resource):
    @jwt_required
    def post(self, *args, **kwargs):
        from_path, to_path, iscut = JU.extract_keys(request.get_json(), "from", "to", "iscut")
        if None in [from_path, to_path]:
            return JU.make_response("paths missing", 400)
        
        if not os.path.exists(from_path):
            return JU.make_response("path: '{}' doesn't exists".format(from_path), 400)
        
        if not os.path.exists(to_path):
            return JU.make_response("path: '{}' doesn't exists".format(to_path), 400)
        
        # construct full absolute path
        to_path = os.path.join(to_path, os.path.basename(from_path))
        
        try:
            if iscut:
                shutil.move(from_path, to_path)
            else:
                shutil.copy(from_path, to_path)
        except Exception as e:
            return JU.make_response("Error occured: {}".format(str(e)), 500)
        
        return JU.make_response("file copied", 200)
    
class DeleteFile(Resource):
    @jwt_required
    def delete(self, *args, **kwargs):
        path = JU.extract_keys(request.get_json(), "path")
        if not path:
            return JU.make_response("paths missing", 400)
        
        if not os.path.exists(path):
            return JU.make_response("paths doesn't exists", 400)
        
        try:
            if os.path.isfile(path):
                os.remove(path)
            else:
                shutil.rmtree(path)
        except Exception as e:
            return JU.make_response("Error occured: {}".format(str(e)), 500)
        
        return JU.make_response("file deleted", 200)
    
class RenameFile(Resource):
    @jwt_required
    def post(self, *args, **kwargs):
        path, newname = JU.extract_keys(request.get_json(), "path", "newname")
        if None in [path, newname]:
            return JU.make_response("data missing", 400)
        
        if not os.path.exists(path):
            return JU.make_response("paths doesn't exists", 400)
        
        try:
            os.rename(path, os.path.join(os.path.dirname(path), newname))
        except Exception as e:
            return JU.make_response("Error occured: {}".format(e), 500)
        
        return JU.make_response("file rename successfull", 200)