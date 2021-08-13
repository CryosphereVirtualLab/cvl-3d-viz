import os
import sys
import json
import time
import sys
import traceback
import threading
import queue
import io
import stat
from http.server import BaseHTTPRequestHandler
try:
    from http.server import ThreadingHTTPServer
except:
    from http.server import HTTPServer
    import socketserver
    class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
        daemon = True
        daemon_threads = True
import ssl
from urllib.parse import urlparse, parse_qs
import argparse
from timeseries_db import tsdb
import gzip

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
        self.timeseries_paths = options.timeseries
        self.connections = {}
        self.objects = {}
        self.next_id = 1
        self.next_object_id = 1
        self.queries = []
        self.persist_path = options.persist
        self.lock = threading.Lock()
        self.op_queue = queue.Queue()
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
        self.port = options.port
        host = "localhost"
        if options.any:
        	host = "0.0.0.0"
        ssl_context = None
        if options.ssl and options.cert and options.key:
            ssl_context = ssl.SSLContext()
            ssl_context.check_hostname = False
            ssl_context.load_cert_chain(certfile=options.cert, keyfile=options.key)
        self.http_server = ThreadingHTTPServer((host, self.port), WebHandler)
        if ssl_context != None:
            self.http_server.socket = ssl_context.wrap_socket(self.http_server.socket, server_side=True)
            print("SSL enabled")
        self.http_thread = self.start(self.http_server.serve_forever)
        self.op_thread = self.start(self.op_queue_thread)
        
    def get_timeseries_databases(self):
        thread_locals = threading.local()
        try:
            return thread_locals.timeseries
        except:
            thread_locals.timeseries = []
            if len(options.timeseries) > 0:
                for db in self.timeseries_paths:
                    thread_locals.timeseries.append(tsdb(db))
        return thread_locals.timeseries
        
    
    def handle_timeseries_query(self, qs):
        events = []
        t0 = float(qs["t0"][0]) if "t0" in qs else float(qs["startts"][0])
        t1 = float(qs["t1"][0]) if "t1" in qs else float(qs["endts"][0])
        for db in self.get_timeseries_databases():
            items = db.get(t0, t1)
            for item in items:
                ts, modified, path, type, content = item
                if path is None:
                    path = f"{db.name}/{ts}"
                # Would be nice if we could avoid deserializing the json, just to serialize it again shortly.
                events.append({"ts": ts, "db" : db.name, "path" : path, "type" : type, "content" : json.loads(content)})
        return events
    
    def handle_info_query(self):
        props = []
        for db in self.get_timeseries_databases():
            ts_props = json.loads(db.get_properties())
            ts_props["db"] = db.name
            props.append(ts_props)
        return props
    
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
    
    def op_queue_thread(self):
        while True:
            try:
                op = self.op_queue.get(block=True, timeout=0.5)
                if op["name"] == "add_connection":
                    self._add_connection(op["data"])
                elif op["name"] == "remove_connection":
                    self._remove_connection(op["data"])
                elif op["name"] == "post":
                    self._post(op["data"])
                elif op["name"] == "update":
                    self._update(op["data"]["key"], op["data"]["metadata"], op["data"]["data"])
                elif op["name"] == "msg":
                    self._handle_msg(op["data"]["conn"], op["data"]["data"])
                elif op["name"] == "add_query":
                    self.queries.append(op["data"])
                elif op["name"] == "clean_queries":
                    self._clean_queries()
                else:
                    print(f"Unrecognized op: {op['name']}")
            except queue.Empty:
                pass
    
    def create_op(self, name, data):
        return { "name" : name, "data" : data }
    
    def _add_connection(self, conn):
        self.connections[conn.address] = conn
        id_message = { "key" : self.next_id,
                       "operation" : "id",
                       "meta" : None }
        conn.sendMessage(json.dumps(id_message))
        self.next_id += 1
    
    def _remove_connection(self, conn):
        try:
            del self.connections[conn.address]
        except:
            traceback.print_exc()
        self._clean_queries()

    def _post(self, data):
        to_remove = []
        for addr, conn in self.connections.items():
            try:
                conn.sendMessage(data)
            except:
                to_remove.append(conn)
                traceback.print_exc()
        for conn in to_remove:
            self._remove_connection(conn)
    
    def add_connection(self, conn):
        self.op_queue.put(self.create_op("add_connection", conn))
    
    def remove_connection(self, conn):
        self.op_queue.put(self.create_op("remove_connection", conn))
    
    def post(self, notification):
        data = json.dumps(notification)
        self.op_queue.put(self.create_op("post", data))
    
    def update(self, key, metadata=None, data=None):
        if self.read_only:
            print("Read-only, ignoring update")
            return
        data = { "key" : key, "metadata" : metadata, "data" : data }
        self.op_queue.put(self.create_op("update", data))
    
    def _update(self, key, metadata=None, data=None):
        notification = { "key" : key,
                         "operation" : None,
                         "meta" : None }
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
        # TODO: Use object.lock here? Shouldn't be necessary, everything is serialized through op_queue
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
        responses = QueryResponses(len(self.connections))
        self.clean_queries()
        self.add_query(responses)
        self.post(notification)
        return responses
    
    def handle(self, conn, data):
        msg = { "conn" : conn, "data" : data }
        self.op_queue.put(self.create_op("msg", msg))
    
    def add_query(self, query):
        self.op_queue.put(self.create_op("add_query", query))
    
    def clean_queries(self):
        self.op_queue.put(self.create_op("clean_queries", None))
    
    def _handle_msg(self, conn, data):
        for q in self.queries:
            if q.add_response(conn, data):
                return
        print(f"Got unhandled message from {conn}: {data}")
        self._clean_queries()
    
    def _clean_queries(self):
        if len(self.queries) > 0:
            to_erase = []
            for q in self.queries:
                if not q.valid():
                    to_erase.append(q)
            for q in to_erase:
                self.queries.remove(q)
        
manager = None
    
class WebHandler(BaseHTTPRequestHandler):
    def __init__(self, *args):
        self.lock = threading.Lock()
        BaseHTTPRequestHandler.__init__(self, *args)
    
    def send_mime(self, response, mimetype, code=200):
        self.send_response(code)
        self.send_header("Content-Type", mimetype)
        if len(response) > 1024 and "Accept-Encoding" in self.headers and "gzip" in self.headers["Accept-Encoding"]:
            compressed = self.compress(response)
            if compressed is not None:
                response = compressed
                self.send_header("Content-Encoding", "gzip")
        self.send_header("Content-Length", str(len(response)))
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        if len(response) > 0:
            self.wfile.write(response)
    
    def compress(self, response):
        compressed = gzip.compress(response)
        if len(compressed) < len(response):
            return compressed
    
    def send_json(self, response):
        self.send_mime(bytes(response, encoding="utf-8"), "application/json")
        
    def send_404(self):
        self.send_mime(bytes("Not found", encoding="utf-8"), "text/plain", 404)
    
    def load_data(self):
        content_length	= int(self.headers["Content-Length"])
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
            elif comps[0] == "events":
                self.enter_event_mode()
            elif comps[0] == "ts":
                self.send_json(json.dumps(manager.handle_timeseries_query(qs)))
            elif comps[0] == "info":
                self.send_json(json.dumps(manager.handle_info_query()))
            elif comps[0] == "trust":
                response = "Congratulations, you have successfully trusted the server's self-signed certificate! You may now close this tab."
                self.send_mime(bytes(response, encoding="utf-8"), "text/plain", 200)
            else:
                self.send_404()
        except:
            traceback.print_exc()
            self.send_404()
    
    def enter_event_mode(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.flush()
        self.address = self.client_address
        manager.add_connection(self)
    
    def sendMessage(self, msg):
        with self.lock:
            #print(f"Sending: {msg}")
            for line in msg.split("\n"):
                to_send = "data: " + line + "\n"
                #print(f"  {to_send}")
                self.wfile.write(to_send.encode("utf-8"))
            self.wfile.write("\n\n".encode("utf-8"))
            self.wfile.flush()
    
    def do_OPTIONS(self):
        # Here to handle CORS. So we basically say everything is ok, then
        # let the browser go ahead and do the real post.
        self.send_response(204)
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Origin, Content-Type, Accept")
        self.send_header("Allow", "OPTIONS, GET, POST")
        self.end_headers()
        self.wfile.flush()
    
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
            elif operation == "state":
                manager.handle(self, post_data)
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

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="CVL Object Server")
    parser.add_argument("--read-only", default=False, action="store_true", help="Run in read-only mode")
    parser.add_argument("--persist", default=None, action="store", help="Path to directory where objects will be stored. If not specified, objects disappear when the server restarts.")
    parser.add_argument("--port", default=3193, type=int, help="Port number the web server will listen on")
    parser.add_argument("--any", default=False, action="store_true", help="Allow connections from any interface")
    parser.add_argument("--timeseries", nargs="*", default=[], help="Timeseries databases to serve data from")
    parser.add_argument("--ssl", default=True, action=argparse.BooleanOptionalAction, help="Enable SSL support")
    parser.add_argument("--cert", default="cert.pem", help="Path to certificate file for SSL")
    parser.add_argument("--key", default="key.pem", help="Path to private key file for SSL")
    options = parser.parse_args()
    if options.ssl:
        try:
            s = os.stat(options.cert)
            s = os.stat(options.key)
        except:
            print("SSL is enabled by default, but no certificate or key has been configured. Use --no-ssl to disable SSL.")
            print("To generate a self-signed certificate for localhost, execute the following command:\n")
            
            print("  openssl req -x509 -nodes -days 730 -newkey rsa:2048 -keyout key.pem -out cert.pem -config localhost-ssl.conf\n")
            
            print("Alternately, edit the localhost-ssl.conf file to generate a self-signed certificate for a different hostname/IP")
            print("address, and run the command above. You will also need to configure your web browser to trust the self-signed")
            print("certificate.")
            raise SystemExit
    manager = DataConnectionManager(options)
    try:
        while True:
            time.sleep(10)
    except:
        raise SystemExit

