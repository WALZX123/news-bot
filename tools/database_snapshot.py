"""Explicit MySQL snapshots and restore-to-new-schema, never overwrite a DB."""
from pathlib import Path
from datetime import date, datetime, timezone
import argparse
import json
import os
import re
import sys
import mysql.connector

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.config import load_config


def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_]{1,64}',value):
        raise ValueError('Invalid database/table identifier')
    return '`'+value+'`'


def connect(cfg, database=None):
    values=dict(host=cfg.host,port=cfg.port,user=cfg.user,password=cfg.password,
                charset=cfg.charset,use_pure=True,connection_timeout=10,time_zone='+00:00')
    if database is not None:
        values['database']=database
    return mysql.connector.connect(**values)


def snapshot_database(cfg, destination):
    path=Path(destination).resolve()
    if path.exists():
        raise ValueError('Choose a new backup filename; existing snapshots are never overwritten')
    path.parent.mkdir(parents=True,exist_ok=True)
    conn=connect(cfg,cfg.database)
    cur=None
    try:
        cur=conn.cursor()
        cur.execute('START TRANSACTION WITH CONSISTENT SNAPSHOT')
        cur.execute('SHOW TABLES')
        names=[row[0] for row in cur.fetchall()]
        result={'format':2,'database':cfg.database,'created_at_utc':datetime.now(timezone.utc).isoformat(),'tables':{}}
        for name in names:
            quoted=identifier(name)
            cur.execute('SHOW CREATE TABLE '+quoted)
            create=cur.fetchone()[1]
            cur.execute('SELECT * FROM '+quoted)
            columns=[item[0] for item in cur.description]
            result['tables'][name]={'create_sql':create,'columns':columns,'rows':cur.fetchall()}
        content=json.dumps(result,ensure_ascii=False,indent=2,default=lambda value:value.isoformat() if isinstance(value,(datetime,date)) else str(value))
        fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
        with os.fdopen(fd,'w',encoding='utf-8') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        return {'tables':len(names),'rows':sum(len(v['rows']) for v in result['tables'].values())}
    finally:
        conn.rollback()
        if cur is not None:
            cur.close()
        conn.close()


def restore_to_new_database(cfg, source, database):
    identifier(database)
    if database==cfg.database:
        raise ValueError('Restoring over the configured database is forbidden')
    data=json.loads(Path(source).read_text(encoding='utf-8'))
    if data.get('format') not in (1,2) or not isinstance(data.get('tables'),dict):
        raise ValueError('Not a supported snapshot')
    conn=connect(cfg)
    cur=None
    try:
        cur=conn.cursor()
        cur.execute('SELECT COUNT(*) FROM information_schema.schemata WHERE schema_name=%s',(database,))
        if cur.fetchone()[0]:
            raise ValueError('Restore destination already exists; nothing was changed')
        cur.execute('CREATE DATABASE '+identifier(database)+' CHARACTER SET utf8mb4')
        cur.execute('USE '+identifier(database))
        for name,table in data['tables'].items():
            identifier(name)
            statement=table['create_sql']
            # Snapshots are trusted operator artifacts, not an upload format.
            # Reject multi-statements or names inconsistent with the manifest.
            if ';' in statement or not statement.startswith('CREATE TABLE '+identifier(name)+' '):
                raise ValueError('Unexpected table definition in snapshot')
            cur.execute(statement)
            rows=table['rows']
            if not rows:
                continue
            columns=table.get('columns') or list(rows[0])
            values=[list(row.values()) if isinstance(row,dict) else row for row in rows]
            cur.executemany('INSERT INTO '+identifier(name)+' ('+','.join(identifier(c) for c in columns)+') VALUES ('+','.join(['%s']*len(columns))+')',values)
        conn.commit()
        return {'database':database,'tables':len(data['tables']),'rows':sum(len(t['rows']) for t in data['tables'].values())}
    finally:
        if cur is not None:
            cur.close()
        conn.close()


def main():
    parser=argparse.ArgumentParser(description='私密数据库备份；仅恢复到一个全新数据库')
    parser.add_argument('--config')
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--backup',metavar='NEW_JSON_PATH')
    mode.add_argument('--restore',metavar='TRUSTED_JSON_PATH')
    parser.add_argument('--new-database')
    args=parser.parse_args()
    cfg=load_config(args.config).mysql
    if args.backup:
        result=snapshot_database(cfg,args.backup)
    else:
        if not args.new_database:
            parser.error('--restore requires --new-database')
        result=restore_to_new_database(cfg,args.restore,args.new_database)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
