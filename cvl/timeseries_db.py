import os
import sys
import traceback
import time
import sqlite3

class tsdb:
    def __init__(self, db):
        self.db = db
        self.name = os.path.basename(db)
        self.conn = sqlite3.connect(db, isolation_level=None)
        self.cursor = self.conn.cursor()
        self.create_tables()
    
    def __del__(self):
        self.cursor.close()
        self.conn.close()
        self.conn = None
    
    def insert_meta(self, properties, version=1):
        res = self.cursor.execute(f"replace into meta (id, version, properties) values (?, ?, ?);", (1, version, properties))
    
    def get_properties(self):
        return self.cursor.execute(f"select properties from meta where id=1;").fetchone()[0]
    
    def create_tables(self):
        res = self.cursor.execute("""create table if not exists resources (
                                    ts real primary key not null,
                                    modified real,
                                    path text,
                                    type text,
                                    content text);""")
        res = self.cursor.execute("""create table if not exists meta (
                                    id integer primary key,
                                    version integer,
                                    properties text);""")
    
    def insert(self, ts, type, content, overwrite=False):
        modified = time.time()
        method = "replace" if overwrite else "insert"
        res = self.cursor.execute(f"{method} into resources (ts, modified, type, content) values (?, ?, ?, ?);", (ts, modified, type, content))
    
    def get_content(self, t0):
        return self.get(t0)[3]
    
    def get(self, t0, t1=None):
        if t1 is None:
            return self.cursor.execute("select ts, modified, path, type, content from resources where ts = ?;", (t0,)).fetchone()
        return self.cursor.execute("select ts, modified, path, type, content from resources where ts > ? and ts <= ? order by ts asc;", (t0, t1)).fetchall()
    
    def exists(self, t0):
        res = self.cursor.execute("select count(ts) from resources where ts = ?;", (t0,))
        return res.fetchone()[0]
    
    def test(self):
        try:
            res = self.cursor.execute("""insert into resources (ts, modified, json) values (?, ?, ?);""", (1, 1, "[]"))
        except:
            traceback.print_exc()
            pass
        res = self.cursor.execute("""select * from resources order by ts asc;""")
        print("Selecting first time")
        for r in res:
            print(r)
        res = self.cursor.execute("""replace into resources (ts, modified, json) values (?, ?, ?);""", (1, 2, "[]"))
        print("Selecting second time")
        res = self.cursor.execute("""select * from resources order by ts asc;""")
        for r in res:
            print(r)
        

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser("Timeseries DB testing")
    parser.add_argument("--db", required=True, help="Path to database")
    options = parser.parse_args()
    db = tsdb(options.db)
    #db.test()
