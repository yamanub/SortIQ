"""Native Flask integration; no extra daemon, fixed host, or second web port."""
import ipaddress
import json
from pathlib import Path
import re
import socket
from urllib.parse import urlsplit

from flask import Blueprint, Response, jsonify, make_response, request, send_file
from . import service


def register(app, root, run_status, counts_dirty=None):
    service.setup(root,run_status,counts_dirty)
    assets=Path(root)/'webui'/'static'/'autoundefined'
    bp=Blueprint('autoundefined',__name__)

    def trusted():
        host=(urlsplit('http://'+request.host).hostname or '').lower()
        if host in ('localhost',socket.gethostname().lower()) or host.endswith('.local'):
            return True
        try:
            address=ipaddress.ip_address(host)
            return address.is_private or address.is_loopback
        except ValueError:
            return False

    def guarded(function):
        from functools import wraps
        @wraps(function)
        def handle(*args,**kwargs):
            if not trusted():return jsonify(error='Use the local SortIQ address for AutoUndefined.'),403
            if request.method=='POST':
                if request.headers.get('Origin') not in (None,request.host_url.rstrip('/')) or request.headers.get('Sec-Fetch-Site')=='cross-site':
                    return jsonify(error='Cross-origin request blocked.'),403
                if not request.is_json or request.content_length is None or not 0<request.content_length<=8192:
                    return jsonify(error='Same-origin JSON required (maximum 8192 bytes).'),400
                if not isinstance(request.get_json(silent=True),dict):
                    return jsonify(error='Expected a JSON object.'),400
            try:
                response=make_response(function(*args,**kwargs))
            except (ValueError,KeyError,TypeError) as error:
                response=make_response(jsonify(error=str(error)),400)
            except (OSError,RuntimeError):
                response=make_response(jsonify(error='Operation unavailable. Existing key, originals and transaction archives are retained.'),503)
            response.headers['Cache-Control']='no-store'
            response.headers['X-Content-Type-Options']='nosniff'
            response.headers['Referrer-Policy']='same-origin'
            return response
        return handle

    @bp.route('/api/autoundefined/<action>',methods=['GET','POST'])
    @bp.route('/autoundefined/api/<action>',methods=['GET','POST'])
    @guarded
    def api(action):
        if request.method=='GET':
            if action=='config':return jsonify(service.configuration())
            if action=='state':return jsonify(service.snapshot())
            return jsonify(error='Not found.'),404
        body=request.get_json(silent=True)
        if not isinstance(body,dict):raise ValueError('Expected a JSON object.')
        if action in ('config','settings'):return jsonify(service.configure(body))
        if action=='scan':return jsonify(imported=service.scan())
        if action=='select-class':
            return jsonify(service.select_class(body.get('id'),body.get('stamp'),body.get('profile')))
        if action=='file-all':
            return jsonify(service.file_all(body.get('profile')))
        if action=='file':
            return jsonify(service.file_case(body.get('id'),body.get('stamp'),body.get('profile'),body.get('confirmed')))
        if action in ('undo','recover'):
            if body.get('confirmed') is not True:raise ValueError('Confirm this operation.')
            return jsonify((service.undo if action=='undo' else service.recover_case)(body.get('id')))
        if action=='retry':
            with service.LOCK,service.db() as c:
                row=service.getcase(body.get('id'))
                if row['state']!='error':raise ValueError('Only a failed API request can be retried.')
                c.execute("UPDATE cases SET state='queued',error=NULL,retry_at=0 WHERE id=?",(row['id'],))
                service.event(c,row['id'],'retry_requested')
            return jsonify(ok=True)
        return jsonify(error='Not found.'),404

    @bp.get('/api/autoundefined/image/<ident>')
    @bp.get('/autoundefined/api/image/<ident>')
    @guarded
    def image(ident):
        if not re.fullmatch('[a-f0-9]{32}',ident):return jsonify(error='Not found.'),404
        row=service.getcase(ident)
        path=service.SORTIQ/row['source']
        if not path.resolve().is_relative_to(service.SORTIQ.resolve()):return jsonify(error='Not found.'),404
        if not path.is_file():path=service.STORE/'archive'/ident/Path(row['source']).name
        if not path.is_file():return jsonify(error='Image no longer available.'),404
        return send_file(path,mimetype='image/png',conditional=False)

    @bp.get('/autoundefined/')
    @guarded
    def review():return send_file(assets/'index.html')

    @bp.get('/autoundefined/review.js')
    @guarded
    def script():return send_file(assets/'review.js',mimetype='application/javascript')

    app.register_blueprint(bp)

    def proposal_rows():return service.snapshot()['cases']

    @app.after_request
    def enrich(response):
        if request.method!='GET' or response.status_code!=200 or not response.is_json:return response
        match=re.fullmatch(r'/api/runs/(run_\d{8}_\d{6})/rejects',request.path)
        if not match and request.path!='/api/tray':return response
        try:
            rows=proposal_rows();data=response.get_json()
            if match:
                for case in data.get('rejects',[]):
                    row=next((r for r in rows if r['run']==match[1] and r['n']==case['n'] and r['available']),None)
                    if row:case['ai_proposal']=row['proposal']
            else:
                profile=service.profile_state()[0]
                for group in data.get('groups',[]):
                    for case in group['cases']:
                        row=next((r for r in rows if r['kind']=='tray' and r['n']==case['n'] and r['profile']==profile and r['available']),None)
                        if row:case['ai_proposal']=row['proposal']
            response.set_data(json.dumps(data))
        except (OSError,ValueError,KeyError):pass  # Native review still works if optional state is unavailable.
        return response

    original=app.view_functions['api_run_reject_resolve']
    @guarded
    def resolve(run_id,n):
        body=request.get_json(silent=True) or {}
        if body.get('action')=='save' and body.get('stamp') and body['stamp']!='UNKNOWN':
            rows=proposal_rows()
            row=next((r for r in rows if r['run']==run_id and r['n']==n and r['available']),None)
            if row and row['proposal']:
                return jsonify(service.file_case(row['id'],body['stamp'],service.profile_state()[0],True))
        return original(run_id,n)
    app.view_functions['api_run_reject_resolve']=resolve

    original_tray=app.view_functions['api_tray_resolve']
    @guarded
    def resolve_tray():
        body=request.get_json(silent=True) or {}
        if body.get('action')=='save':
            profile,model,_=service.profile_state();rows=proposal_rows()
            wanted=[r for r in rows if r['kind']=='tray' and r['n'] in body.get('ns',[]) and r['profile']==profile and r['available']]
            if body.get('stamp') in model.get('stamp_labels',[]) and wanted and all(r['proposal'] for r in wanted):
                for row in wanted:service.file_case(row['id'],body['stamp'],profile,True)
                return jsonify(ok=True,stamp=body['stamp'],saved=len(wanted))
        return original_tray()
    app.view_functions['api_tray_resolve']=resolve_tray
    return service
