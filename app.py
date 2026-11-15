"""Gate5 PC lending. Python 3.10+, standard library only."""
from __future__ import annotations
import argparse
import calendar
import contextlib
from datetime import datetime, timedelta, timezone, date
from html import escape
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.cookies
import json
import logging
from pathlib import Path
import re
import secrets
import sqlite3
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
JST = timezone(timedelta(hours=9))
LOG = logging.getLogger('gate5')
MESSAGES = {
 'maintenance': '現在、サーバーのメンテナンスをおこなっています。貸出・返却はメンテナンス終了後にするか、IT資産管理システム 開発チームまでお問い合わせください。',
 'required': '一部データの入力がありません。再度入力してから、申請してください。',
 'past': '返却予定日が過去の日付になっています。再度、返却予定日を入力し直してください。',
 'future': '返却予定日は申請日から1ヶ月後までしか選択できません。再度、返却予定日を入力し直してください。',
 'missing': 'この端末は存在していません。別の端末を選択してください。',
 'LENT': 'この端末は現在貸出中です。別の端末を選択してください。',
 'REPAIR': 'この端末は現在修理中です。別の端末を選択してください。',
 'DISPOSED': 'この端末は廃棄済みです。別の端末を選択してください。',
 'LEAVE': '休職中のため、貸し出すことができません。',
 'RETIRED': '既に退職しているため、貸し出すことができません。',
 'limit': '別の端末を貸出中のため、貸し出すことができません。貸出中の端末を返却してから、申請してください。',
 'overdue': 'この端末は返却予定日を過ぎています。IT資産管理システム 開発チームまでご連絡ください。',
 'flow': 'エラーが発生しました。最初からやり直してください。',
 'storage': '更新途中にエラーが発生しました。もう一度初めからやり直してください。',
}

class RuleError(Exception):
 def __init__(self, message, status=400):
  self.message, self.status = message, status
  super().__init__(message)

class Store:
 def __init__(self, path, config=ROOT / 'config.json', clock=None, fault=None):
  self.path, self.config = str(path), Path(config)
  self.clock = clock or (lambda: datetime.now(JST))
  self.fault = fault  # Test-only injection; never exposed by HTTP.
 def now(self):
  return self.clock().astimezone(JST)
 def connect(self):
  db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
  db.row_factory = sqlite3.Row
  db.execute('PRAGMA foreign_keys = ON')
  db.execute('PRAGMA busy_timeout = 10000')
  return db
 @contextlib.contextmanager
 def read(self):
  db = self.connect()
  try: yield db
  finally: db.close()
 @contextlib.contextmanager
 def tx(self):
  with self.read() as db:
   db.execute('BEGIN IMMEDIATE')
   try:
    yield db
    db.commit()
   except BaseException:
    db.rollback()
    raise
 def init(self):
  with self.read() as db:
   db.execute('PRAGMA journal_mode = WAL')
   db.executescript('''
CREATE TABLE IF NOT EXISTS employees (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL CHECK(length(name)<=50),
 department TEXT NOT NULL CHECK(length(department)<=50),
 employment_status TEXT NOT NULL CHECK(employment_status IN ('ACTIVE','LEAVE','RETIRED')));
CREATE TABLE IF NOT EXISTS devices (
 id INTEGER PRIMARY KEY, asset_no TEXT UNIQUE NOT NULL CHECK(length(asset_no)<=20),
 model_name TEXT NOT NULL CHECK(length(model_name)<=50),
 device_type TEXT NOT NULL CHECK(device_type IN ('LAPTOP','TABLET','MONITOR')),
 status TEXT NOT NULL CHECK(status IN ('AVAILABLE','LENT','REPAIR','DISPOSED')), purchased_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS lendings (
 id INTEGER PRIMARY KEY, device_id INTEGER NOT NULL REFERENCES devices(id),
 user_id INTEGER NOT NULL REFERENCES employees(id), lent_at TEXT NOT NULL,
 due_date TEXT NOT NULL, returned_at TEXT,
 purpose TEXT NOT NULL CHECK(length(purpose) BETWEEN 1 AND 100));
CREATE UNIQUE INDEX IF NOT EXISTS one_device ON lendings(device_id) WHERE returned_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS one_employee ON lendings(user_id) WHERE returned_at IS NULL;
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES employees(id), csrf TEXT NOT NULL, expires TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS operations (
 token TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES sessions(id), kind TEXT NOT NULL,
 payload TEXT NOT NULL, expires TEXT NOT NULL, result TEXT);
''')
  with self.tx() as db:
   if db.execute('SELECT count(*) FROM employees').fetchone()[0]: return
   db.executemany('INSERT INTO employees VALUES (?,?,?,?)', [
    (1001,'山田 太郎','営業部','ACTIVE'), (1002,'佐藤 花子','開発部','ACTIVE'),
    (1003,'鈴木 一郎','総務部','LEAVE'), (1004,'田中 美咲','企画部','RETIRED'),
    (1005,'高橋 健','開発部','ACTIVE')])
   db.executemany('INSERT INTO devices VALUES (?,?,?,?,?,?)', [
    (1,'PC-0001','ThinkPad X1 Carbon','LAPTOP','AVAILABLE','2025-04-01'),
    (2,'PC-0002','MacBook Air 13','LAPTOP','AVAILABLE','2025-06-15'),
    (3,'PC-0003','Surface Laptop 6','LAPTOP','LENT','2025-05-01'),
    (4,'PC-0004','Latitude 7450','LAPTOP','REPAIR','2024-08-01'),
    (5,'PC-0005','ThinkPad T14','LAPTOP','DISPOSED','2020-04-01'),
    (6,'TB-0001','iPad Air','TABLET','AVAILABLE','2025-04-01')])
   today = self.now().date()
   db.execute('INSERT INTO lendings(device_id,user_id,lent_at,due_date,purpose) VALUES (3,1005,?,?,?)',
    ((self.now()-timedelta(days=2)).isoformat(),(today+timedelta(days=5)).isoformat(),'社外研修'))
 def maintenance(self):
  try:
   data = json.loads(self.config.read_text(encoding='utf-8'))
   if type(data.get('maintenance')) is not bool: raise ValueError('maintenance must be bool')
   if data['maintenance']: raise RuleError(MESSAGES['maintenance'],503)
  except (OSError, ValueError) as exc:
   LOG.error('Invalid configuration: %s', exc)
   raise RuleError('システム設定を読み込めません。管理者にお問い合わせください。',503) from exc
 def session(self, sid, db=None):
  if db is None:
   with self.read() as conn: return self.session(sid,conn)
  row = db.execute('SELECT s.*, e.name,e.department,e.employment_status FROM sessions s JOIN employees e ON e.id=s.user_id WHERE s.id=?',(sid,)).fetchone()
  if not row or row['expires'] <= self.now().isoformat(): raise RuleError('ログインの有効期限が切れました。再度ログインしてください。',401)
  return dict(row)
 def login(self, employee_id, name, department):
  if not all((employee_id,name,department)): raise RuleError(MESSAGES['required'])
  if not re.fullmatch(r'[0-9]{1,18}',employee_id): raise RuleError('社員IDは半角数字で記入してください。')
  with self.tx() as db:
   emp = db.execute('SELECT * FROM employees WHERE id=?',(int(employee_id),)).fetchone()
   if not emp: raise RuleError('社員IDが一致しません。再度、入力し直してください。')
   if name.strip()!=emp['name']: raise RuleError('氏名が一致しません。再度、入力し直してください。')
   if department.strip()!=emp['department']: raise RuleError('所属部署が一致しません。再度、入力し直してください。')
   sid,csrf = secrets.token_urlsafe(32),secrets.token_urlsafe(32)
   db.execute('INSERT INTO sessions VALUES (?,?,?,?)',(sid,emp['id'],csrf,(self.now()+timedelta(hours=8)).isoformat()))
  return sid
 def logout(self,sid):
  with self.tx() as db:
   db.execute('DELETE FROM operations WHERE session_id=?',(sid,))
   db.execute('DELETE FROM sessions WHERE id=?',(sid,))
 def validate(self, db, user_id, data):
  asset,due,purpose = (data.get(k,'').strip() for k in ('asset_no','due_date','purpose'))
  if not all((asset,due,purpose)): raise RuleError(MESSAGES['required'])
  if len(asset)>20 or not re.fullmatch(r'[A-Z0-9]+(?:-[A-Z0-9]+)*',asset):
   raise RuleError('資産番号は半角英大文字・半角数字・ハイフンで記入してください。')
  if len(purpose)>100: raise RuleError('利用目的は100文字以内で入力してください。')
  try:
   if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',due,re.ASCII): raise ValueError()
   day=date.fromisoformat(due)
  except ValueError: raise RuleError('返却予定日は存在する日付を入力してください。')
  today=self.now().date()
  year,month = (today.year+1,1) if today.month==12 else (today.year,today.month+1)
  maximum=date(year,month,min(today.day,calendar.monthrange(year,month)[1]))
  if day<today: raise RuleError(MESSAGES['past'])
  if day>maximum: raise RuleError(MESSAGES['future'])
  device=db.execute('SELECT * FROM devices WHERE asset_no=?',(asset,)).fetchone()
  if not device: raise RuleError(MESSAGES['missing'],404)
  if device['device_type']!='LAPTOP': raise RuleError('貸出対象はノートPCのみです。別の端末を選択してください。')
  if device['status']!='AVAILABLE': raise RuleError(MESSAGES[device['status']],409)
  emp=db.execute('SELECT * FROM employees WHERE id=?',(user_id,)).fetchone()
  if not emp: raise RuleError('社員IDが一致しません。再度、入力し直してください。')
  if emp['employment_status']!='ACTIVE': raise RuleError(MESSAGES[emp['employment_status']],403)
  if db.execute('SELECT 1 FROM lendings WHERE user_id=? AND returned_at IS NULL',(user_id,)).fetchone(): raise RuleError(MESSAGES['limit'],409)
  return dict(asset_no=asset,due_date=due,purpose=purpose,device_id=device['id'],model_name=device['model_name'])
 def prepare(self,sid,data,kind='lend'):
  self.maintenance()
  with self.tx() as db:
   sess=self.session(sid,db)
   if kind=='lend': payload=self.validate(db,sess['user_id'],data)
   else:
    payload=self.validate_return(db,sess['user_id'],data.get('lending_id',''))
   token=secrets.token_urlsafe(32)
   db.execute('INSERT INTO operations VALUES (?,?,?,?,?,NULL)',(token,sid,kind,json.dumps(payload,ensure_ascii=False),(self.now()+timedelta(minutes=15)).isoformat()))
   return token,payload
 def operation(self,sid,token,db=None):
  if db is None:
   with self.read() as conn:
    self.session(sid,conn)
    return self.operation(sid,token,conn)
  row=db.execute('SELECT * FROM operations WHERE token=? AND session_id=?',(token,sid)).fetchone()
  if not row or (not row['result'] and row['expires']<=self.now().isoformat()): raise RuleError(MESSAGES['flow'],403)
  return dict(row)
 def validate_return(self,db,user_id,lending_id):
  if not re.fullmatch(r'[0-9]{1,18}',str(lending_id),re.ASCII): raise RuleError('この端末は貸出していません。')
  row=db.execute('SELECT l.*,d.asset_no,d.model_name,d.status FROM lendings l JOIN devices d ON d.id=l.device_id WHERE l.id=? AND l.user_id=?',(int(lending_id),user_id)).fetchone()
  if not row: raise RuleError('この端末は貸出していません。',404)
  if row['returned_at']: raise RuleError('この端末は既に返却済みです。',409)
  if self.now().date()>date.fromisoformat(row['due_date']): raise RuleError(MESSAGES['overdue'],409)
  if row['status']!='LENT': raise RuleError('端末と貸出履歴の状態が一致しません。管理者にお問い合わせください。',409)
  return dict(lending_id=row['id'],asset_no=row['asset_no'],model_name=row['model_name'],due_date=row['due_date'],device_id=row['device_id'])
 def commit(self,sid,token):
  with self.tx() as db:
   sess=self.session(sid,db)
   op=self.operation(sid,token,db)
   # A committed result is returned even if maintenance was enabled after success.
   if op['result']: return json.loads(op['result'])
   self.maintenance()
   payload=json.loads(op['payload'])
   if op['kind']=='lend':
    payload=self.validate(db,sess['user_id'],payload)
    cur=db.execute('INSERT INTO lendings(device_id,user_id,lent_at,due_date,purpose) VALUES (?,?,?,?,?)',
     (payload['device_id'],sess['user_id'],self.now().isoformat(),payload['due_date'],payload['purpose']))
    lending_id=cur.lastrowid
    if self.fault: self.fault()
    db.execute("UPDATE devices SET status='LENT' WHERE id=?",(payload['device_id'],))
   else:
    payload=self.validate_return(db,sess['user_id'],payload['lending_id'])
    lending_id=payload['lending_id']
    db.execute('UPDATE lendings SET returned_at=? WHERE id=?',(self.now().isoformat(),lending_id))
    if self.fault: self.fault()
    db.execute("UPDATE devices SET status='AVAILABLE' WHERE id=?",(payload['device_id'],))
   result={**payload, 'kind':op['kind'], 'lending_id':lending_id}
   db.execute('UPDATE operations SET result=? WHERE token=?',(json.dumps(result,ensure_ascii=False),token))
   return result
 def dashboard(self,sid):
  with self.read() as db:
   sess=self.session(sid,db)
   devices=[dict(r) for r in db.execute("SELECT * FROM devices WHERE device_type='LAPTOP' ORDER BY asset_no")]
   loans=[dict(r) for r in db.execute('SELECT l.*,d.asset_no,d.model_name FROM lendings l JOIN devices d ON d.id=l.device_id WHERE user_id=? ORDER BY l.id DESC',(sess['user_id'],))]
   return sess,devices,loans

STATUS={'AVAILABLE':('貸出可能','available'),'LENT':('貸出中','lent'),'REPAIR':('修理中','repair'),'DISPOSED':('廃棄済み','disposed')}
def h(value): return escape(str(value),quote=True)
def badge(status):
 label,css=STATUS[status]
 return f'<span class="badge {css}"><i></i>{label}</span>'
def hidden(name,value): return f'<input type="hidden" name="{h(name)}" value="{h(value)}">'

def page(title,body,sess=None,active='lend'):
 user=f'<div class="user"><span class="avatar">{h(sess["name"][0])}</span><div><strong>{h(sess["name"])}</strong><small>{h(sess["department"])} · {sess["user_id"]}</small></div></div>' if sess else '<div class="user"><span class="avatar">G</span><div><strong>研修用デモ</strong><small>サンプル社員で利用できます</small></div></div>'
 nav='<a class="navitem '+('selected' if active=='lend' else '')+'" href="/">▦ <span>PC貸出申請</span></a><a class="navitem '+('selected' if active=='return' else '')+'" href="/returns">↩ <span>返却・貸出履歴</span></a>' if sess else '<span class="navitem selected">↗ <span>ログイン</span></span>'
 logout=f'<form method="post" action="/logout">{hidden("csrf",sess["csrf"])}<button class="logout">ログアウト →</button></form>' if sess else ''
 return f'''<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{h(title)} | Gate5</title><link rel="stylesheet" href="/static/style.css"><script defer src="/static/app.js"></script></head><body><aside><a href="/" class="brand"><span class="brandicon">G<span>5</span></span><span>Gate5<small>IT ASSET MANAGEMENT</small></span></a><div class="navlabel">WORKSPACE</div>{nav}<div class="sidebarbottom"><div class="help"><strong>共用PCを、スムーズに。</strong><p>貸出は1人1台まで。<br>期限内の返却にご協力ください。</p></div>{user}{logout}</div></aside><div class="workspace"><header><span>ワークスペース <b>/</b> {h(title)}</span><span class="env">● 研修用環境</span></header><main>{body}</main><footer>Gate5 · IT資産管理システム<span>お問い合わせ：IT資産管理システム 開発チーム</span></footer></div></body></html>'''

def alert(message): return f'<div class="alert" role="alert"><span class="alerticon">!</span><div><strong>内容をご確認ください</strong><p>{h(message)}</p></div></div>' if message else ''
def heading(eyebrow,title,subtitle): return f'<div class="heading"><div><div class="eyebrow">{h(eyebrow)}</div><h1>{h(title)}</h1><p>{h(subtitle)}</p></div><span class="headingicon">▱</span></div>'

class Handler(BaseHTTPRequestHandler):
 server_version='Gate5'
 def log_message(self,fmt,*args): LOG.info(fmt,*args)
 @property
 def store(self): return self.server.store
 def sid(self):
  cookie=http.cookies.SimpleCookie()
  try: cookie.load(self.headers.get('Cookie',''))
  except http.cookies.CookieError: return ''
  return cookie['gate5'].value if 'gate5' in cookie else ''
 def send(self,body,status=200,headers=None,content_type='text/html; charset=utf-8'):
  data=body.encode('utf-8') if isinstance(body,str) else body
  self.send_response(status)
  self.send_header('Content-Type',content_type)
  self.send_header('Content-Length',str(len(data)))
  self.send_header('Cache-Control','no-store')
  self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'")
  self.send_header('X-Content-Type-Options','nosniff')
  self.send_header('Referrer-Policy','same-origin')
  for k,v in (headers or {}).items(): self.send_header(k,v)
  self.end_headers()
  try: self.wfile.write(data)
  except (BrokenPipeError,ConnectionResetError): pass
 def redirect(self,url,headers=None): self.send('',303,{'Location':url,**(headers or {})})
 def login_page(self,error='',status=200):
  with self.store.read() as db: employees=[dict(r) for r in db.execute('SELECT * FROM employees ORDER BY id')]
  rows=''
  for e in employees:
   label={'ACTIVE':'在職中','LEAVE':'休職中','RETIRED':'退職済み'}[e['employment_status']]
   rows+=f'<button type="button" class="sample" data-id="{e["id"]}" data-name="{h(e["name"])}" data-department="{h(e["department"])}"><span class="avatar">{h(e["name"][0])}</span><span><strong>{h(e["name"])}</strong><small>{e["id"]} · {h(e["department"])}</small></span><span class="sample-status">{label}</span></button>'
  body=heading('WELCOME TO GATE5','ワークスペースにログイン','サンプル社員を選んで、PCの貸出・返却をはじめましょう。')+alert(error)+f'''<div class="twocol"><section class="panel"><div class="paneltitle"><span class="step">01</span><h2>社員情報</h2></div><form method="post" action="/login"><label>社員ID <em>必須</em><input name="employee_id" inputmode="numeric" maxlength="18" placeholder="1001" required></label><label>氏名 <em>必須</em><input name="name" maxlength="50" placeholder="山田 太郎" required></label><label>所属部署 <em>必須</em><input name="department" maxlength="50" placeholder="営業部" required></label><button class="primary wide">ログイン →</button></form></section><section class="panel"><div class="paneltitle"><span class="step">02</span><h2>サンプル社員を選択</h2></div><p class="muted">選択すると左の社員情報が入力されます。</p><div class="samples">{rows}</div><p class="hint">研修用の簡易ログインです。実在する社員情報やパスワードを入力しないでください。</p></section></div>'''
  self.send(page('ログイン',body),status)
 def home(self,error='',status=200,values=None):
  sess,devices,loans=self.store.dashboard(self.sid())
  values=values or {}
  available=sum(d['status']=='AVAILABLE' for d in devices)
  current=[l for l in loans if not l['returned_at']]
  options='<option value="">端末を選択してください</option>'+''.join(f'<option value="{h(d["asset_no"])}" {"selected" if values.get("asset_no")==d["asset_no"] else ""}>{h(d["asset_no"])} / {h(d["model_name"])} · {STATUS[d["status"]][0]}</option>' for d in devices)
  cards=''.join(f'<article class="device"><div class="device-top"><span class="laptop">▱</span>{badge(d["status"])}</div><h3>{h(d["model_name"])}</h3><div class="asset">{h(d["asset_no"])}</div><div class="device-foot"><span>ノートPC</span><span>{"選択して申請 ↓" if d["status"]=="AVAILABLE" else "現在は利用できません"}</span></div></article>' for d in devices)
  warning='<div class="notice">現在、貸出可能な端末はありません。時間をおいてご確認ください。</div>' if not available else ''
  if current: warning+=f'<div class="notice">現在 {h(current[0]["asset_no"])} を貸出中です。新しい申請の前に、<a href="/returns">返却手続き</a>を行ってください。</div>'
  body=heading('DEVICE LENDING','必要なPCを、必要なときに。','共用ノートPCの空き状況を確認して、貸出を申請できます。')+alert(error)
  try: self.store.maintenance()
  except RuleError as exc: body+='<div class="notice">'+h(exc.message)+'</div>'
  body+=f'''<div class="stats"><div><span>管理中のPC</span><strong>{len(devices)}<small>台</small></strong></div><div><span><i class="dot green"></i>貸出可能</span><strong>{available}<small>台</small></strong></div><div><span><i class="dot blue"></i>あなたの貸出</span><strong>{len(current)}<small>/ 1台</small></strong></div><div><span>貸出期間</span><strong class="period">最大1か月<small>当日返却も可能</small></strong></div></div><div class="sectionline"><h2>端末の一覧 <span>{len(devices)}</span></h2><span class="muted">最新の状態は申請確定時に再確認されます</span></div><div class="devices">{cards}</div>{warning}<div class="application"><section class="panel"><div class="paneltitle"><span class="step">01</span><h2>貸出申請</h2><span class="muted">入力 → 確認 → 完了</span></div><form method="post" action="/prepare">{hidden('csrf',sess['csrf'])}<label>貸出端末 <em>必須</em><select name="asset_no" required>{options}</select></label><div class="fieldrow"><label>返却予定日 <em>必須</em><input type="date" name="due_date" value="{h(values.get('due_date',''))}" required><small>本日から1か月後まで選択できます。</small></label><label>借用者<input value="{h(sess['name'])} / {h(sess['department'])}" readonly></label></div><label>利用目的 <em>必須</em><textarea name="purpose" maxlength="100" rows="3" placeholder="例：社外研修への参加のため" required>{h(values.get('purpose',''))}</textarea><small>日本語で入力できます。100文字以内。</small></label><div class="formbottom"><span class="muted">確認画面で内容をご確認ください。</span><button class="primary" {"disabled" if not available else ""}>貸出する →</button></div></form></section><section class="guide"><span class="guideicon">◎</span><h2>ご利用の前に</h2><ol><li><strong>貸出は1人1台まで</strong><p>別の端末をご希望の場合は、先に返却してください。</p></li><li><strong>返却予定日は必ず入力</strong><p>最大1か月まで。当日中の返却も可能です。</p></li><li><strong>期限を過ぎた場合</strong><p>画面からは返却できません。開発チームへご連絡ください。</p></li></ol></section></div>'''
  self.send(page('PC貸出申請',body,sess),status)
 def returns(self,error='',status=200):
  sess,_,loans=self.store.dashboard(self.sid())
  rows=''
  for l in loans:
   overdue=not l['returned_at'] and l['due_date']<self.store.now().date().isoformat()
   state='返却済み' if l['returned_at'] else ('期限超過' if overdue else '貸出中')
   action=f'<form action="/return/prepare" method="post">{hidden("csrf",sess["csrf"])}{hidden("lending_id",l["id"])}<button class="secondary">返却する →</button></form>' if not l['returned_at'] else '—'
   rows+=f'<tr><td><strong>{h(l["model_name"])}</strong><small>{h(l["asset_no"])}</small></td><td>{h(l["due_date"])}</td><td><span class="badge {"repair" if overdue else "lent" if not l["returned_at"] else "available"}">{state}</span></td><td>{h(l["purpose"])}</td><td>{h(l["returned_at"][:10]) if l["returned_at"] else "—"}</td><td>{action}</td></tr>'
  body=heading('YOUR LENDINGS','返却・貸出履歴','利用が終わった端末はこちらから返却してください。')+alert(error)+f'<section class="panel"><h2>あなたの貸出履歴</h2><div class="tablewrap"><table><thead><tr><th>端末</th><th>返却予定日</th><th>状態</th><th>利用目的</th><th>返却日</th><th>操作</th></tr></thead><tbody>{rows}</tbody></table></div>'+(f'<div class="empty">現在、貸出履歴はありません。<a href="/">PCを借りる →</a></div>' if not loans else '')+'</section><p class="hint">返却予定日の23:59:59（日本時間）まで返却できます。期限超過時は開発チームにご連絡ください。</p>'
  self.send(page('返却・貸出履歴',body,sess,'return'),status)
 def confirmation(self,token):
  sess=self.store.session(self.sid())
  op=self.store.operation(self.sid(),token)
  if op['result']: return self.redirect('/result?token='+token)
  p=json.loads(op['payload'])
  returning=op['kind']=='return'
  title='返却内容の確認' if returning else '貸出内容の確認'
  body=heading('CONFIRMATION',title,'以下の内容でよろしければ、確定ボタンを押してください。')
  pairs=[('資産番号',p['asset_no']),('機種名',p['model_name']),('借用者',sess['name']),('返却予定日',p['due_date'])]
  if not returning: pairs.append(('利用目的',p['purpose']))
  body+='<section class="panel narrow"><div class="progress">01 入力 <b>→ 02 確認</b> → 03 完了</div><dl>'+''.join(f'<dt>{h(k)}</dt><dd>{h(v)}</dd>' for k,v in pairs)+'</dl>'
  body+=f'<form method="post" action="/commit">{hidden("csrf",sess["csrf"])}{hidden("token",token)}<div class="formbottom"><a href="{"/returns" if returning else "/"}">← 戻る</a><button class="primary">{"返却を確定" if returning else "貸出を確定"} →</button></div></form></section>'
  self.send(page(title,body,sess,'return' if returning else 'lend'))
 def result(self,token):
  sess=self.store.session(self.sid())
  op=self.store.operation(self.sid(),token)
  if not op['result']: raise RuleError(MESSAGES['flow'],403)
  p=json.loads(op['result'])
  returning=p['kind']=='return'
  title='返却が完了しました' if returning else '貸出が完了しました'
  message=f'{p["asset_no"]} を返却しました。' if returning else f'{p["asset_no"]} を貸し出しました。返却予定日は {p["due_date"].replace("-","/")} です。'
  body=f'<section class="panel success"><div class="check">✓</div><div class="eyebrow">COMPLETED</div><h1>{title}</h1><p>{h(message)}</p><div class="receipt"><span>対象端末</span><strong>{h(p["model_name"])}</strong><span>資産番号</span><strong>{h(p["asset_no"])}</strong><span>返却予定日</span><strong>{h(p["due_date"])}</strong></div><p class="muted">{"ご利用ありがとうございました。" if returning else "貸出申請を受け付けました。貸し出した端末は、期日までに返却してください。"}</p><a class="primary" href="/returns">返却・貸出履歴を見る →</a><a class="textlink" href="/">貸出申請に戻る</a></section>'
  self.send(page(title,body,sess,'return' if returning else 'lend'))
 def fail(self,exc,path='',data=None):
  if exc.status==401: return self.login_page(exc.message,401)
  try:
   if path in ('/','/prepare'): return self.home(exc.message,exc.status,data)
   if path in ('/returns','/return/prepare'): return self.returns(exc.message,exc.status)
   sess=self.store.session(self.sid())
  except RuleError: sess=None
  self.send(page('処理を完了できませんでした',heading('REQUEST ERROR','処理を完了できませんでした','入力内容や現在の状態を確認して、もう一度お試しください。')+alert(exc.message)+'<a class="secondary" href="/">貸出申請へ戻る →</a><a class="textlink" href="/returns">返却・貸出履歴へ</a>',sess),exc.status)
 def storage_failure(self):
  self.send(page('処理を完了できませんでした',heading('SYSTEM ERROR','処理を完了できませんでした','時間をおいて再度お試しください。')+alert(MESSAGES['storage'])+'<p>通信切断や応答エラー後は、貸出履歴をご確認ください。確定画面からの再試行では二重登録を防止します。</p><a class="secondary" href="/">貸出申請へ戻る →</a>'),503)
 def do_GET(self):
  path=urlsplit(self.path).path
  try:
   if path in ('/static/style.css','/static/app.js'):
    file=ROOT/path.lstrip('/')
    return self.send(file.read_bytes(),content_type='text/css; charset=utf-8' if path.endswith('.css') else 'text/javascript; charset=utf-8')
   if path=='/login': return self.login_page()
   self.store.session(self.sid())
   if path=='/': return self.home()
   if path=='/returns': return self.returns()
   if path in ('/confirm','/result'):
    token=parse_qs(urlsplit(self.path).query).get('token',[''])[0]
    return self.confirmation(token) if path=='/confirm' else self.result(token)
   raise RuleError('ページが見つかりません。',404)
  except RuleError as exc: self.fail(exc,path)
  except (sqlite3.Error,OSError):
   LOG.exception('Storage error')
   self.storage_failure()
 def do_POST(self):
  path=urlsplit(self.path).path
  data={}
  try:
   if self.headers.get('Transfer-Encoding'): raise RuleError('送信形式が正しくありません。',400)
   try: length=int(self.headers.get('Content-Length','0'))
   except ValueError: raise RuleError('送信形式が正しくありません。')
   if not 0<length<=16384: raise RuleError('入力データのサイズが正しくありません。',413)
   if self.headers.get('Content-Type','').split(';')[0]!='application/x-www-form-urlencoded': raise RuleError('送信形式が正しくありません。',415)
   fields=parse_qs(self.rfile.read(length).decode('utf-8'),keep_blank_values=True,max_num_fields=20)
   if any(len(v)!=1 for v in fields.values()): raise RuleError(MESSAGES['flow'])
   data={k:v[0] for k,v in fields.items()}
   origin=self.headers.get('Origin')
   if origin and origin!=f'http://{self.headers.get("Host")}': raise RuleError(MESSAGES['flow'],403)
   if path=='/login':
    sid=self.store.login(data.get('employee_id',''),data.get('name',''),data.get('department',''))
    old=self.sid()
    if old: self.store.logout(old)
    return self.redirect('/',{'Set-Cookie':f'gate5={sid}; HttpOnly; SameSite=Lax; Path=/; Max-Age=28800'})
   sess=self.store.session(self.sid())
   if not secrets.compare_digest(data.get('csrf',''),sess['csrf']): raise RuleError(MESSAGES['flow'],403)
   if path=='/logout':
    self.store.logout(self.sid())
    return self.redirect('/login',{'Set-Cookie':'gate5=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0'})
   if path in ('/prepare','/return/prepare'):
    token,_=self.store.prepare(self.sid(),data,'return' if path=='/return/prepare' else 'lend')
    return self.redirect('/confirm?token='+token)
   if path=='/commit':
    token=data.get('token','')
    self.store.commit(self.sid(),token)
    return self.redirect('/result?token='+token)
   raise RuleError(MESSAGES['flow'],404)
  except RuleError as exc:
   if path=='/login': return self.login_page(exc.message,exc.status)
   self.fail(exc,path,data)
  except (UnicodeError,ValueError): self.fail(RuleError('入力データの形式が正しくありません。'),path)
  except (sqlite3.Error,OSError):
   LOG.exception('Transaction failed')
   self.storage_failure()

def make_server(store,host='127.0.0.1',port=8000):
 server=ThreadingHTTPServer((host,port),Handler)
 server.store=store
 return server

def main():
 parser=argparse.ArgumentParser(description='Gate5 PC貸出管理')
 parser.add_argument('--db',default=str(ROOT/'gate5.sqlite3'))
 parser.add_argument('--config',default=str(ROOT/'config.json'))
 parser.add_argument('--port',type=int)
 args=parser.parse_args()
 logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
 config=json.loads(Path(args.config).read_text(encoding='utf-8'))
 store=Store(args.db,args.config)
 store.init()
 server=make_server(store,config.get('host','127.0.0.1'),args.port if args.port is not None else config.get('port',8000))
 print(f'Gate5 起動: http://{server.server_address[0]}:{server.server_address[1]}',flush=True)
 try: server.serve_forever()
 except KeyboardInterrupt: pass
 finally: server.server_close()
if __name__=='__main__': main()
