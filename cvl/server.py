import os
import sys
import json
import time
import sys
import traceback
import threading
import io
from SimpleWebSocketServer import SimpleWebSocketServer, WebSocket
from http.server import BaseHTTPRequestHandler
try:
    from http.server import ThreadingHTTPServer
except:
    from http.server import HTTPServer
    import socketserver
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon = True
        daemon_threads = True
from urllib.parse import urlparse, parse_qs
import argparse

class CVLObject:
    def __init__(self, key, id):
        self.key = key
        self.data = None
        self.metadata = None
        self.last_data = 0.0
        self.id = id
        self.data_dirty = False
        self.lock = threading.Lock()
    
    def update_metadata(self):
        if self.metadata != None:
            self.metadata["updated"] = time.time()
            if "path" not in self.metadata:
                self.metadata["path"] = ""
            if self.data != None:
                self.metadata["has_data"] = True
            else:
                self.metadata["has_data"] = False
            self.metadata["last_data"] = self.last_data
    
    def persist(self, path):
        object_meta = { "metadata" : self.metadata, "last_data" : self.last_data, "key" : self.key, "id" : self.id }
        with io.open(os.path.join(path, f"{self.id}.meta"), "w", encoding="utf-8") as fd:
            json.dump(object_meta, fd, ensure_ascii=False)
        if self.data is not None and self.data_dirty:
            with open(os.path.join(path, f"{self.id}.data"), "wb") as fd:
                fd.write(self.data)
            self.data_dirty = False
    
    def purge(self, path):
        try:
            os.remove(os.path.join(path, f"{self.id}.meta"))
        except:
            pass
        try:
            os.remove(os.path.join(path, f"{self.id}.data"))
        except:
            pass
        
class QueryResponses:
    MAX_WAIT = 2.0
    def __init__(self, max_expected_replies):
        self.max_expected_replies = max_expected_replies
        self.start = time.time()
        self.responses = []
        self.clients = set()
        self.lock = threading.Condition()
    
    def valid(self):
        return time.time() - self.start < self.MAX_WAIT
    
    def add_response(self, conn, data):
        with self.lock:
            if conn in self.clients:
                # already have a response from this client
                return False
            self.clients.add(conn)
            self.responses.append(data)
            self.lock.notify_all()
        return True
    
    def wait_for_responses(self):
        with self.lock:
            while self.max_expected_replies != len(self.responses) and self.valid():
                timeout = (self.start + self.MAX_WAIT) - time.time()
                if timeout > 0:
                    self.lock.wait(timeout)
                else:
                    break
        return self.responses

class DataConnectionManager:
    def __init__(self, options):
        self.connections = {}
        self.objects = {}
        self.next_id = 1
        self.next_object_id = 1
        self.queries = []
        self.persist_path = options.persist
        self.lock = threading.Lock()
        self.read_only = options.read_only
        if options.persist is None:
            print("Server is running in transient mode - any objects created will be lost on server exit.")
        else:
            try:
                os.makedirs(options.persist, exist_ok=True)
            except:
                traceback.print_exc()
                print("Failed to make root directory for persisting objects")
                self.persist_path = None
        if self.persist_path is not None:
            self.load_objects()
        self.base_port = options.base_port
        host = "localhost"
        if options.any:
        	host = "0.0.0.0"
        self.ws_server = SimpleWebSocketServer(host, self.base_port + 1, WebSocketHandler)
        self.http_server = ThreadingHTTPServer((host, self.base_port), WebHandler)
        self.ws_thread = self.start(self.ws_server.serveforever)
        self.http_thread = self.start(self.http_server.serve_forever)
    
    def load_objects(self):
        files = os.listdir(self.persist_path)
        loaded = set()
        print("Loading existing objects..")
        for f in files:
            try:
                oid, ext = f.split(".")
                object_id = int(oid)
                if object_id in loaded:
                    continue
                self.next_object_id = max(object_id, self.next_object_id)
                with io.open(os.path.join(self.persist_path, f"{object_id}.meta"), "r", encoding="utf-8") as fd:
                    meta = json.load(fd)
                try:
                    with open(os.path.join(self.persist_path, f"{object_id}.data"), "rb") as fd:
                        data = fd.read()
                except:
                    data = None
                object = CVLObject(meta["key"], object_id)
                self.objects[meta["key"]] = object
                object.metadata = meta["metadata"]
                object.last_data = meta["last_data"]
                object.data = data
                loaded.add(object_id)
            except:
                traceback.print_exc()
                print(f"Failed to laod this item: {f}.")
        self.next_object_id += 1
        print(f"Loaded {len(self.objects)} objects")
    
    def start(self, target):
        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        return thread
    
    def add_connection(self, conn):
        self.connections[conn.address] = conn
        id_message = { "key" : self.next_id,
                       "operation" : "id",
                       "meta" : None }
        conn.sendMessage(json.dumps(id_message))
        self.next_id += 1
    
    def remove_connection(self, conn):
        del self.connections[conn.address]
        self.clean_queries()
    
    def post(self, notification):
        data = json.dumps(notification)
        for addr, conn in self.connections.items():
            try:
                conn.sendMessage(data)
            except:
                traceback.print_exc()
    
    def update(self, key, metadata=None, data=None):
        notification = { "key" : key,
                         "operation" : None,
                         "meta" : None }
        if self.read_only:
            print("Read-only, ignoring update")
            return
        with self.lock:
            if metadata is None and data is None:
                # Delete the key
                notification["operation"] = "delete"
                if key in self.objects:
                    self.objects[key].purge(self.persist_path)
                    del self.objects[key]
                self.post(notification)
                return
            notification["operation"] = "update"
            if key not in self.objects:
                self.objects[key] = CVLObject(key, self.next_object_id)
                self.next_object_id += 1
        object = self.objects[key]
        with object.lock:
            if metadata is not None:
                object.metadata = metadata
            if data is not None:
                object.data = data
                object.last_data = time.time()
                object.data_dirty = True
            object.update_metadata()
            if self.persist_path is not None:
                object.persist(self.persist_path)
            # Only post notifications once an object has valid metadata.
            if object.metadata != None:
                self.post(notification)
    
    def control(self, metadata):
        notification = { "key" : None,
                         "operation" : "control",
                         "meta" : metadata }
        self.post(notification)
    
    def query(self):
        notification = { "key" : None,
                         "operation" : "query",
                         "meta" : None }
        self.clean_queries()
        responses = QueryResponses(len(self.connections))
        self.queries.append(responses)
        self.post(notification)
        return responses
    
    def handle(self, conn, data):
        for q in self.queries:
            if q.add_response(conn, data):
                return
        print(f"Got unhandled message from {conn}: {data}")
        self.clean_queries()
    
    def clean_queries(self):
        if len(self.queries) > 0:
            to_erase = []
            for q in self.queries:
                if not q.valid():
                    to_erase.append(q)
            for q in to_erase:
                self.queries.remove(q)
        
manager = None
    
class WebHandler(BaseHTTPRequestHandler):
    def send_mime(self, response, mimetype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", mimetype)
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if len(response) > 0:
            self.wfile.write(response)
    
    def send_json(self, response):
        self.send_mime(bytes(response, encoding="utf-8"), "application/json")
        
    def send_404(self):
        self.send_mime(bytes("Not found", encoding="utf-8"), "text/plain", 404)
    
    def load_data(self):
        content_length	= int(self.headers['Content-Length'])
        data			= self.rfile.read(content_length)
        return data
    
    def get_key(self, qs=None):
        if qs and "key" in qs:
            return qs["key"][0]
        return self.headers["X-CVL-Object-Key"]
    
    def parse_url(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query, keep_blank_values=True)
        comps = parsed.path[1:].split("/")
        return (comps, qs)
    
    def load_post_data(self):
        try:
            return json.loads(self.load_data())
        except:
            return None
    
    def do_GET(self):
        try:
            """
            Basic API for getting data (meta is default and need not be specified):
            GET /object?[meta || data][&key=<key>]
            
            The key can also be sent as an HTTP-header (X-CVL-Object-Key)
            """
            comps, qs = self.parse_url()
            if comps[0] == "object":
                key = self.get_key(qs)
                if "meta" in qs or not "data" in qs:
                    self.send_json(json.dumps(manager.objects[key].metadata))
                elif "data" in qs:
                    self.send_mime(manager.objects[key].data, "application/octet-stream")
                else:
                    self.send_404()
            elif comps[0] == "list":
                self.send_json(json.dumps(list(filter(lambda x: manager.objects[x].metadata != None, manager.objects.keys()))))
            else:
                self.send_404()
        except:
            traceback.print_exc()
            self.send_404()
        
    def do_POST(self):
        global manager
        if manager.read_only:
            self.send_404()
            return
        try:
            comps, qs = self.parse_url()
            key = self.get_key()
            post_data = self.load_post_data()
            if len(comps) != 1:
                self.send_404()
                return
            operation = comps[0]
            result = { "success" : True }
            if operation == "publish":
                print(f"publish {post_data}")
                manager.update(key, post_data, None)
                self.send_json(json.dumps(result))
            elif operation == "delete":
                manager.update(key, None, None)
                self.send_json(json.dumps(result))
            elif operation == "control":
                manager.control(post_data)
                self.send_json(json.dumps(result))
            elif operation == "query":
                responses = manager.query()
                result = responses.wait_for_responses()
                self.send_json(json.dumps(result))
            else:
                self.send_404()
        except:
            traceback.print_exc()
            self.send_404()
    
    def do_PUT(self):
        global manager
        if manager.read_only:
            self.send_404()
            return
        try:
            comps, qs = self.parse_url()
            key = self.get_key()
            data = self.load_data()
            #print(f"Loaded {len(data)} bytes of data for key '{key}'")
            result = { "success" : True }
            operation = comps[0]
            if operation == "publish" and len(data) > 0:
                manager.update(key, None, data)
                self.send_json(json.dumps(result))
            else:
                self.send_404()
        except:
            traceback.print_exc()
            self.send_404()

class WebSocketHandler(WebSocket):
    def handleMessage(self):
        try:
            data = json.loads(self.data)
            manager.handle(self, data)
        except:
            traceback.print_exc()
    
    def handleConnected(self):
        print(f"+ Connect {self.address}")
        manager.add_connection(self)
    
    def handleClose(self):
        print(f"- Closing {self.address}")
        manager.remove_connection(self)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CVL Object Server")
    parser.add_argument("--read-only", default=False, action="store_true", help="Run in read-only mode")
    parser.add_argument("--persist", default=None, action="store", help="Path to directory where objects will be stored. If not specified, objects disappear when the server restarts.")
    parser.add_argument("--base-port", default = 3193, type=int, help="Base port number for web server. Web sockets use the given port number + 1.")
    parser.add_argument("--any", default=False, action="store_true", help="Allow connections from any interface")
    options = parser.parse_args()
    manager = DataConnectionManager(options)
    try:
        while True:
            time.sleep(10)
    except:
        manager.ws_server.close()
        raise SystemExit

