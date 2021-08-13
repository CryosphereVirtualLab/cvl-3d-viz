#!/usr/bin/env python3

import os
import sys
import requests
import traceback
import numpy as np
import osgeo.osr as osr
import osgeo.gdal as gdal
import math
import threading
import queue
import time
import json
import urllib3

# We are not going to verify certificates, and will typically connect to servers
# with self-signed certs.
urllib3.disable_warnings()

#import logging
#logging.basicConfig(level=logging.DEBUG)

must_set_axis_mapping = int(gdal.VersionInfo()) > 3 * 1000000

def reproject(buffer, projection):
    if projection == 4326:
        return buffer
    else:
        src = osr.SpatialReference()
        src.ImportFromEPSG(projection)
        tgt = osr.SpatialReference()
        tgt.ImportFromEPSG(4326)
        if must_set_axis_mapping:
            tgt.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        xform = osr.CoordinateTransformation(src, tgt)
        transformed = xform.TransformPoints(buffer)
        return np.array(transformed)


"""
The viz_3d class is provided as a convenience for rendering georeferenced
"stuff" in 3D, and marshalling the necessary data into an object suitable
for transfer to the CVL server.
"""
class VBO:
    VERTEX = 0
    COLOR = 1
    TEXCOORD = 2
    NORMAL = 3
    INDEX = 4
    NUM_BUFFERS = INDEX+1
    TYPES = [ "vertex", "color", "texcoord", "normal", "index", "texture" ]
    PRIMITIVES = [ "points", "lines", "linestrip", "lineloop", "triangles", "trianglestrip" ]
    
    def __init__(self, primitive="points", projection=32633):
        self.primitive = primitive # Valid primitives are points, lines, linestrip, triangles, trianglestrip and raster
        if self.primitive not in self.PRIMITIVES:
            raise Exception(f"Error, primitive {self.primitive} is unknown. Valid primitives are {self.PRIMITIVES}")
        self.geo_center = None
        self.source_buffers = [ None for i in range(0, self.NUM_BUFFERS) ]
        self.processed_buffers = [ None for i in range(0, self.NUM_BUFFERS) ]
        self.projection = projection
        self.texture = None
        self.offset = np.zeros((3,))
        self.compute_normals = False
        self.nan_to_zero = False
        self.nan_check = False
    
    @staticmethod
    def rgb_to_color(red, green, blue, alpha=255):
        return ((alpha & 0xff) << 24) | ((blue & 0xff) << 16) | ((green & 0xff) << 8) | (red & 0xff)
    
    @staticmethod
    def rgb_to_color_no_mask(red, green, blue, alpha=255):
        return (alpha << 24) | (blue << 16) | (green << 8) | (red)
    
    @staticmethod
    def gen_normals(vertices, indices):
        normals = np.zeros((vertices.shape[0], 3), dtype=np.float32)
        for i in range(0, indices.shape[0], 3):
            v0 = vertices[indices[i]] - vertices[indices[i+1]]
            v1 = vertices[indices[i]] - vertices[indices[i+2]]
            n = np.cross(v0, v1)
            normals[indices[i]] += n
            normals[indices[i+1]] += n
            normals[indices[i+2]] += n
        for i in range(0, normals.shape[0]):
            normals[i] /= np.sqrt(np.sum(normals[i]**2))
        return normals

    
    def llh_to_spherical(self):
        buf = self.processed_buffers[self.VERTEX]
        earthRadiusMeters			= 6378137.0
        earthSphericalRadius		= 128.0
        earthSphericalFromMeters	= earthSphericalRadius/earthRadiusMeters
        to_radians                  = math.pi/180.0
        cos = math.cos
        sin = math.sin
        sqrt = math.sqrt
        for i in range(0, buf.shape[0]):
            if self.nan_check and np.any(np.isinf(buf[i])):
                if self.nan_to_zero:
                    buf[i,0] = 0
                    buf[i,1] = 0
                    buf[i,2] = 0
                else:
                    raise Exception("Vertex buffer contains infinity")
            x, y, z = buf[i]
            height = earthSphericalRadius + (z * earthSphericalFromMeters)
            cosLat = cos(to_radians*y)
            cosLon = cos(to_radians*x)
            signX		= -1.0 if x < 0.0 else 1.0
            signY		= -1.0 if y < 0.0 else 1.0
            sinLat		= sqrt(1.0-cosLat*cosLat) * signY
            sinLon		= sqrt(1.0-cosLon*cosLon) * signX
            buf[i]      = (height * cosLat * sinLon, height * sinLat, height * cosLat * cosLon)
    
    def compute_geo_center(self):
        self.geo_center = np.mean(self.processed_buffers[self.VERTEX], axis=0)
        centered = self.processed_buffers[self.VERTEX] - self.geo_center
        self.processed_buffers[self.VERTEX] = centered.astype(np.float32)
    
    def set_vertex(self, buffer):
        if buffer is not None and buffer.dtype != np.dtype(np.float32) and buffer.dtype != np.dtype(np.float64):
            raise Exception("Vertex buffer must be of type numpy.float32 or numpy.float64")
        self.source_buffers[self.VERTEX] = buffer
    
    def set_texcoord(self, buffer):
        if buffer is not None and buffer.dtype != np.dtype(np.float32):
            raise Exception("Texcoord buffer must be of type numpy.float32")
        self.source_buffers[self.TEXCOORD] = buffer
    
    def set_normal(self, buffer):
        if buffer is not None and buffer.dtype != np.dtype(np.float32):
            raise Exception("Normal buffer must be of type numpy.float32")
        self.source_buffers[self.NORMAL] = buffer

    def set_color(self, buffer):
        if buffer is not None and buffer.dtype != np.dtype(np.uint32) and buffer.dtype != np.dtype(np.uint8):
            raise Exception("Color buffer must be of type numpy.uint32 (abgr on little endian) or numpy.uint8 (rgba)")
        self.source_buffers[self.COLOR] = buffer
    
    def set_index(self, buffer):
        if buffer is not None and buffer.dtype != np.dtype(np.uint32):
            raise Exception("Index buffer must be of type numpy.uint32")
        self.source_buffers[self.INDEX] = buffer
    
    def validate(self):
        if self.source_buffers[self.INDEX] is not None and self.primitive in ["triangles", "trianglestrip"]:
            if self.primitive == "triangles" and self.source_buffers[self.INDEX].shape[0] % 3 != 0:
                raise Exception("Index buffer length is not divisible by 3")
            elif self.primitive == "trianglestrip" and self.source_buffers[self.INDEX].shape[0] < 3:
                raise Exception("Index buffer is too short")
        vertex_count = self.source_buffers[self.VERTEX].shape[0]
        for i in range(1, self.INDEX):
            if self.source_buffers[i] is not None and self.source_buffers[i].shape[0] != vertex_count:
                if i == self.COLOR and self.source_buffers[i].shape[0] != 4*vertex_count or i != self.COLOR:
                    raise Exception(f"Buffer {i} has too few/too many elements: {self.source_buffers[i].shape[0]} should be {vertex_count}")
        
    def prepare(self):
        """
        This function does several things:
            1. Reproject the data to lat-lon
            2. Convert lat-lon to spherical coords used by visualization
            3. Compute a geo center for the vertex data
            3. Subtract geo center and convert the reprojected vertices to float32
            4. Bundle everything up in a giant bytes() array with corresponding metadata 
        """
        self.validate()
        if np.count_nonzero(self.offset) != 0:
            print(f"Offsetting array by {self.offset}")
            self.source_buffers[self.VERTEX] += self.offset
        self.processed_buffers[self.VERTEX] = reproject(self.source_buffers[self.VERTEX], self.projection)
        self.llh_to_spherical()
        if self.compute_normals:
            if self.source_buffers[self.INDEX] is None:
                # TODO: Should just use the implicit index in this case (ie, triplets of verts define a triangle)
                raise Exception("Asked to compute normals, but no index buffer present")
            self.source_buffers[self.NORMAL] = self.gen_normals(self.processed_buffers[self.VERTEX], self.source_buffers[self.INDEX])
        self.compute_geo_center()
        for i in range(self.COLOR, self.NUM_BUFFERS):
            self.processed_buffers[i] = self.source_buffers[i]
        self.data = self.geo_center.tobytes(order="C")
        self.meta = []
        for i in range(0, self.NUM_BUFFERS):
            if self.processed_buffers[i] is not None:
                md = viz.get_npy_metadata(self.processed_buffers[i])
                data = self.processed_buffers[i].tobytes(order="C")
                md["length"] = len(data)
                md["usage"] = self.TYPES[i]
                self.meta.append(md)
                self.data += data
            else:
                self.meta.append(None)
        #print(f"Meta: {self.meta}, Bytes: {len(self.data)}")
        if self.texture is not None:
            self.data += self.texture
            self.meta.append({ "length" : len(self.texture), "usage" : "texture" })
        else:
            self.meta.append(None)

class Raster:
    def __init__(self, grid, grid_dimensions, projection=32633, path=None, image_data=None):
        if path:
            with open(path, "rb") as fd:
                self.image_data = fd.read()
        else:
            self.image_data = image_data
        self.meta = { "grid_dimensions" : [grid_dimensions[0], grid_dimensions[1]] }
        self.grid = grid
        self.projection = projection
        self.grid_dimensions = grid_dimensions
    
    def prepare(self):
        self.reprojected_grid = reproject(self.grid, self.projection)
        self.reprojected_grid = np.array(self.reprojected_grid[:, 0:2]);
        self.data = self.reprojected_grid.tobytes(order="C")
        self.meta["grid_length"] = len(self.data)
        self.data += self.image_data
        self.image_data = None
        
    
class viz:
    def __init__(self, server="localhost", port=3193, proto="https://", threaded=True, num_threads=4):
        self.server = server
        self.port = port
        self.proto = proto
        self.threaded = threaded
        self.num_threads = num_threads
        if self.threaded:
            self.process_queue = queue.Queue(maxsize=20)
            self.publish_queue = queue.Queue(maxsize=20)
            self.process_threads = []
            for i in range(0, self.num_threads):
                pt = threading.Thread(target=self.process_thread, daemon=True)
                pt.start()
                self.process_threads.append(pt)
            self.publish_threads = []
            for i in range(0, self.num_threads):
                pt = threading.Thread(target=self.publish_thread, daemon=True)
                pt.start()
                self.publish_threads.append(pt)
        else:
            self.publish_vbo = self._publish_vbo
            self.publish_raster = self._publish_raster
            self.publish_geojson = self._publish_geojson
        self.session = requests.Session()
    
    def __del__(self):
        if self.threaded:
            # Poison the process threads, which will in turn poison the publish threads.
            for i in range(0, self.num_threads):
                self.process_queue.put(None)
            # Ideally we should join() the threads instead of polling the queues.
            self.wait()
    
    def process_thread(self):
        while True:
            item = self.process_queue.get()
            if item is None:
                break
            method = None
            if item["type"] == "vbo":
                method = self._publish_vbo
            elif item["type"] == "raster":
                method = self._publish_raster
            elif item["type"] == "geojson":
                method = self._publish_geojson
            method(item["key"], item["metadata"], item["object"])
        # For every process thread that exits, kill one of the publish threads too.
        self.publish_queue.put(None)
    
    def publish_thread(self):
        session = requests.Session()
        while True:
            item = self.publish_queue.get()
            if item is None:
                break
            self.post(session, item)
    
    def build_url(self, endpoint):
        return f"{self.proto}{self.server}:{self.port}/{endpoint}"
    
    def generate_headers(self, key, is_metadata):
        return { "X-CVL-Object-Key" : key,
                 "X-CVL-Object-Metadata" : "1" if is_metadata else "0" }
    
    @staticmethod
    def create_item(url, headers, json, data, method):
        return { "url" : url,
                 "headers" : headers,
                 "json" : json,
                 "data" : data,
                 "method" : method }
    
    # look_at takes a position and target as triplets of [lon, lat, height]
    def look_at(self, position, target, duration=0.5):
        md = { "look_at" : { "position" : position,
                             "target" : target,
                             "duration" : duration } }
        self.post(self.session, self.create_item(self.build_url("control"), None, md, None, "post"))
    
    def set_time(self, timestamp):
        md = { "timestamp" : timestamp }
        self.post(self.session, self.create_item(self.build_url("control"), None, md, None, "post"))
    
    def set_time_window(self, window_size):
        md = { "time_window" : window_size }
        self.post(self.session, self.create_item(self.build_url("control"), None, md, None, "post"))
    
    def query(self):
    	r = self.session.post(self.build_url("query"))
    	if r.status_code != requests.codes.ok:
            try:
                r.raise_for_status()
            except:
                traceback.print_exc()
    	else:
    		return r.json()
    
    @staticmethod
    def get_npy_metadata(array):
        md = { "shape" : list(array.shape),
               "type" : array.dtype.str }
        return md
    
    def populate_npy_metadata(self, metadata, array):
        if metadata:
            metadata["numpy"] = self.get_npy_metadata(array)
    
    def publish_npy_array(self, key, metadata, array):
        data = None
        if array:
            data = array.tobytes(order="C")
        self.publish(key, metadata, data)
    
    def publish_vbo(self, key, metadata, vbo):
        item = { "type" : "vbo",
                 "key" : key,
                 "metadata" : metadata,
                 "object" : vbo }
        self.process_queue.put(item)
    
    def publish_raster(self, key, metadata, raster):
        item = { "type" : "raster",
                 "key" : key,
                 "metadata" : metadata,
                 "object" : raster }
        self.process_queue.put(item)
    
    def publish_geojson(self, key, metadata):
        item = { "type" : "geojson",
                 "key" : key,
                 "metadata" : metadata,
                 "object" : None }
        self.process_queue.put(item)
    
    def _publish_vbo(self, key, metadata, vbo):
        vbo.prepare()
        metadata["vbo"] = vbo.meta
        metadata["primitive"] = vbo.primitive
        self.publish(key, metadata, vbo.data)
    
    def _publish_raster(self, key, metadata, raster):
        raster.prepare()
        metadata["raster"] = raster.meta
        self.publish(key, metadata, raster.data)
    
    def _publish_geojson(self, key, metadata, object=None):
        geojson = metadata["geojson"]
        metadata["geojson"] = True
        data = json.dumps(geojson, ensure_ascii=False).encode("utf-8")
        self.publish(key, metadata, data)
    
    def publish(self, key, metadata, data):
        try:
            url = self.build_url("publish")
            # Post data first, so we don't end up with two roundtrips for updates on the server side.
            if data is not None:
                item = self.create_item(url, self.generate_headers(key, False), None, data, "put")
                if self.threaded:
                    self.publish_queue.put(item)
                else:
                    self.post(self.session, item)
            if metadata:
                item = self.create_item(url, self.generate_headers(key, True), metadata, None, "post")
                if self.threaded:
                    self.publish_queue.put(item)
                else:
                    self.post(self.session, item)
        except:
            traceback.print_exc()
            raise
    
    def post(self, session, item):
        if item["method"] == "post":
            method = session.post
        elif item["method"] == "put":
            method = session.put
        # Punt on accepting self-signed certificates.
        r = method(item["url"], headers=item["headers"], json=item["json"], data=item["data"], verify=False)
        if r.status_code != requests.codes.ok:
            try:
                r.raise_for_status()
            except:
                traceback.print_exc()
    
    def wait(self):
        if not self.threaded:
            return
        while self.process_queue.qsize() > 0:
            time.sleep(0.1)
        while self.publish_queue.qsize() > 0:
            time.sleep(0.1)
        print("All items posted, hopefully")
