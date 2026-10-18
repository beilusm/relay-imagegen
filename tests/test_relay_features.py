"""Observable offline protocol, revision, cover and UI behaviors."""
import base64
from contextlib import contextmanager
from email.parser import BytesParser
from email.policy import default
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
import unittest
import urllib.error
import urllib.request

import test_relay_imagegen as baseline
ENCODED, relay = baseline.ENCODED, baseline.relay
from PIL import Image


@contextmanager
def advanced_server(mode='images', gate=None):
    calls = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            raw = self.rfile.read(int(self.headers['Content-Length']))
            if self.headers['Content-Type'].startswith('multipart'):
                parts = BytesParser(policy=default).parsebytes(('Content-Type: '+self.headers['Content-Type']+'\r\n\r\n').encode()+raw)
                body = {}
                for part in parts.iter_parts():
                    name = part.get_param('name', header='Content-Disposition')
                    value = part.get_payload(decode=True)
                    body[name] = value if part.get_filename() else value.decode()
            else:
                body = json.loads(raw)
            calls.append({'path': self.path, 'body': body, 'headers': dict(self.headers)})
            image = {'b64_json': ENCODED, 'revised_prompt': '猫 sk-local-test-secret'}
            response = {'id': 'resp_local', 'status': 'completed', 'usage': {'input_tokens': 19, 'output_tokens': 31},
                        'output': [{'type': 'image_generation_call', 'id': 'ig_local', 'result': ENCODED, 'revised_prompt': '改写的猫'}]}
            if 'stream' in mode:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                def event(data):
                    raw = json.dumps(data, ensure_ascii=False).encode()
                    self.wfile.write(b': keepalive\r\n\r\ndata: '+raw+b'\r\n\r\n')
                    self.wfile.flush()
                prefix = 'response.image_generation_call' if 'responses' in mode else 'image_generation'
                event({'type': prefix+'.partial_image', 'partial_image_b64': ENCODED, 'b64_json': ENCODED})
                if gate is not None:
                    gate.wait(timeout=5)
                if mode == 'stream-drop': return
                if mode == 'stream-error':
                    event({'type': 'error', 'message': 'sk-local-test-secret'})
                    return
                if 'responses' in mode:
                    event({'type': 'response.completed', 'response': response})
                else:
                    event({'type': 'image_generation.completed', **image, 'usage': {'output_tokens': 5}})
                    if mode == 'stream-final-error':
                        event({'type': 'error'})
                self.wfile.write(b'data: [DONE]\n\n')
            else:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(response if mode == 'responses' else {'data': [image], 'usage': {'output_tokens': 5}}).encode())
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try: yield f'http://127.0.0.1:{server.server_port}', calls
    finally:
        server.shutdown();server.server_close();thread.join()


class FeatureTests(baseline.RelayTests):
    # Inherit the existing CLI harness without rerunning its tests in this class.
    def image(self, name='source.png', size=(64, 48), alpha=False):
        path = self.root / name
        Image.new('RGBA' if alpha else 'RGB', size, (255, 0, 0, 0) if alpha else (20, 70, 110)).save(path)
        return path

    def edit(self, *args):
        return self.cli('edit', '--prompt', '只修改指定区域', '--output-dir', str(self.root/'out'), *args)

    def test_mask_multipart_and_options(self):
        with advanced_server() as (base, calls):
            self.configure(base)
            code, result = self.edit('--image', str(self.image()), '--mask', str(self.image('mask.png', alpha=True)),
                '--input-fidelity', 'high', '--output-format', 'webp', '--output-compression', '80')
            self.assertEqual(code, 0, result)
            body=calls[0]['body']
            self.assertIn('mask', body)
            self.assertEqual(body['input_fidelity'], 'high')
            self.assertEqual(body['output_compression'], '80')
            self.assertEqual(result['mask']['width'], 64)
            self.assertNotIn('sk-local-test-secret', Path(result['manifest']).read_text())
            self.assertEqual(result['requests'][0]['usage']['output_tokens'], 5)

    def test_mask_json(self):
        with advanced_server() as (base,calls):
            self.configure(base)
            code,result=self.edit('--image',str(self.image()),'--mask',str(self.image('m.png',alpha=True)),'--edit-transport','json')
            self.assertEqual(code,0,result)
            self.assertTrue(calls[0]['body']['mask']['image_url'].startswith('data:image/png;base64,'))

    def test_invalid_masks_never_submit(self):
        with advanced_server() as (base,calls):
            self.configure(base)
            source=self.image()
            for mask in [self.image('opaque.png'),self.image('wrong.png',(32,32),True)]:
                code,result=self.edit('--image',str(source),'--mask',str(mask))
                self.assertEqual(code,1,result)
            self.assertEqual(calls,[])

    def test_new_option_validation_before_requests(self):
        with advanced_server() as (base,calls):
            self.configure(base)
            for options in [('--partial-images','2'),('--output-compression','101'),('--output-compression','40'),('--backend','responses'),('--responses-model','reasoner'),('--backend','responses','--responses-model','reasoner','--n','2')]:
                code,result=self.generate(*options)
                self.assertEqual(code,1,result)
            self.assertEqual(calls,[])

    def test_capability_profile_rejects_unsupported(self):
        with advanced_server() as (base,calls):
            self.configure(base)
            profile=self.root/'profile.json'
            profile.write_text(json.dumps({'stream':False,'background':['opaque'],'max_n':1,'backends':['images']}))
            code,result=self.cli('capabilities','set','--file',str(profile))
            self.assertEqual(code,0,result)
            for opts in [('--stream',),('--background','transparent'),('--n','2'),('--backend','responses','--responses-model','r')]:
                code,result=self.generate(*opts)
                self.assertEqual(code,1,result)
            self.assertEqual(calls,[])
            code,result=self.generate('--n','2','--request-mode','separate')
            self.assertEqual(code,0,result)
            self.assertEqual(len(calls),2)

    def test_images_stream_previews_and_final(self):
        with advanced_server('stream') as (base,calls):
            self.configure(base)
            code,result=self.generate('--stream','--partial-images','2')
            self.assertEqual(code,0,result)
            self.assertEqual(len(result['previews']),1)
            self.assertEqual(result['saved_count'],1)
            self.assertIs(calls[0]['body']['stream'],True)
            self.assertEqual(result['requests'][0]['event_usage'],[{'output_tokens':5}])

    def test_multipart_stream_uses_lowercase_boolean(self):
        with advanced_server('stream') as (base,calls):
            self.configure(base)
            code,result=self.edit('--image',str(self.image()),'--stream')
            self.assertEqual(code,0,result)
            self.assertEqual(calls[0]['body']['stream'],'true')

    def test_stream_failure_preserves_preview_without_retry(self):
        for mode in ['stream-drop','stream-error']:
            with self.subTest(mode=mode), advanced_server(mode) as (base,calls):
                self.configure(base)
                code,result=self.generate('--stream','--n','2','--request-mode','separate')
                self.assertEqual(code,1,result)
                self.assertEqual(len(calls),1)
                self.assertEqual(result['saved_count'],0)
                self.assertEqual(result['unsubmitted_requests'],1)
                self.assertTrue(Path(result['previews'][0]['path']).is_file())

    def test_stream_failure_preserves_completed_image(self):
        with advanced_server('stream-final-error') as (base,calls):
            self.configure(base)
            code,result=self.generate('--stream','--n','2')
            self.assertEqual(code,1,result)
            self.assertEqual(result['saved_count'],1)
            self.assertTrue(Path(result['files'][0]).is_file())
            self.assertEqual(len(calls),1)

    def test_responses_normal_and_stream(self):
        for mode in ['responses','responses-stream']:
            with self.subTest(mode=mode),advanced_server(mode) as (base,calls):
                self.configure(base)
                code,result=self.generate('--backend','responses','--responses-model','orchestrator',*(['--stream'] if 'stream' in mode else []))
                self.assertEqual(code,0,result)
                self.assertEqual(calls[0]['path'],'/v1/responses')
                body=calls[0]['body']
                self.assertEqual(body['model'],'orchestrator')
                self.assertEqual(body['tools'][0]['model'],'image2.5')
                self.assertNotIn('n',body['tools'][0])
                self.assertFalse(body['store'])
                self.assertEqual(result['requests'][0]['response_id'],'resp_local')
                self.assertEqual(result['images'][0]['revised_prompt'],'改写的猫')

    def test_responses_mask_edit(self):
        with advanced_server('responses') as (base,calls):
            self.configure(base)
            code,result=self.edit('--image',str(self.image()),'--mask',str(self.image('m.png',alpha=True)),
                '--backend','responses','--responses-model','orchestrator','--previous-response-id','resp_parent')
            self.assertEqual(code,0,result)
            body=calls[0]['body']
            self.assertEqual(body['previous_response_id'],'resp_parent')
            self.assertEqual(body['tools'][0]['action'],'edit')
            self.assertIn('input_image_mask',body['tools'][0])
            self.assertEqual(body['input'][0]['content'][1]['type'],'input_image')

    def test_session_select_continue_and_undo(self):
        session=self.root/'session.json'
        with advanced_server() as (base,calls):
            self.configure(base)
            code,initial=self.generate('--session',str(session),'--n','2','--request-mode','separate')
            self.assertEqual(code,0,initial)
            result=self.local_command('history','select','--session',str(session),'--revision','2')
            code=0 if result['ok'] else 1
            self.assertEqual(code,0,result)
            code,edited=self.edit('--session',str(session))
            self.assertEqual(code,0,edited)
            self.assertEqual(edited['references'][0]['path'],initial['files'][1])
            self.assertEqual(edited['parent']['image_index'],2)
            result=self.local_command('history','undo','--session',str(session))
            code=0 if result['ok'] else 1
            self.assertEqual(code,0,result)
            self.assertEqual(result['selected_revision'],2)
            self.assertEqual(len(result['revisions']),3)
            self.assertEqual(len(calls),3)

    def test_responses_session_reuses_stored_response_only(self):
        for stored in (False,True):
            with advanced_server('responses') as (base,calls):
                self.configure(base)
                session=self.root/f'session-{stored}.json'
                opts=['--session',str(session),'--backend','responses','--responses-model','orchestrator']
                code,result=self.generate(*opts,*(['--responses-store'] if stored else []))
                self.assertEqual(code,0,result)
                code,result=self.edit(*opts,'--responses-store')
                self.assertEqual(code,0,result)
                self.assertEqual('previous_response_id' in calls[1]['body'],stored)

    def test_from_run_and_session_lock(self):
        with advanced_server() as (base,calls):
            self.configure(base)
            code,initial=self.generate()
            code,result=self.edit('--from-run',initial['manifest'])
            self.assertEqual(code,0,result)
            self.assertEqual(result['references'][0]['path'],initial['files'][0])
            session=self.root/'locked.json'
            session.with_name('locked.json.lock').touch()
            code,result=self.generate('--session',str(session))
            self.assertEqual(code,1,result)
            self.assertEqual(len(calls),2)

    def test_cover_multisize_and_non_destructive(self):
        background=self.image(size=(900,600))
        original=background.read_bytes()
        logo=self.image('logo.png',(30,30),True)
        spec={'background':str(background),'sizes':['1280x720','640x360'], 'formats':['png','jpeg','webp'],
              'texts':[{'text':'猫的周末\n一起去看世界','font_size':78,'height':.5,'shadow':{'dx':2,'dy':3}}],
              'overlays':[{'image':str(logo),'x':.7,'y':.7,'width':.1,'height':.1}]}
        path=self.root/'cover.json';path.write_text(json.dumps(spec,ensure_ascii=False))
        # cover is local and deliberately does not accept credentials/config.
        result=self.local_command('cover','--spec',str(path),'--output-dir',str(self.root/'covers'))
        self.assertTrue(result['ok'],result)
        self.assertEqual(len(result['files']),6)
        for file in result['files']:
            with Image.open(file) as im:
                im.load();self.assertIn(im.size,[(1280,720),(640,360)])
        self.assertEqual(original,background.read_bytes())
        self.assertEqual(result['layouts'][0]['texts'][0]['text'],spec['texts'][0]['text'])

    def local_command(self,*args):
        proc=subprocess.run([sys.executable,str(Path(relay.__file__)),*args],capture_output=True,text=True,env=self.env,timeout=15)
        self.assertEqual(proc.stderr,'',proc.stderr)
        return json.loads(proc.stdout)

    def test_cover_rejects_overflow_and_missing_glyph(self):
        background=self.image(size=(800,600))
        for layer in [{'text':'超长文字'*100,'height':.01,'font_size':40,'min_font_size':40},
                      {'text':'中文','font':str(next(Path('/usr/share/fonts').rglob('DejaVuSans.ttf')))}]:
            path=self.root/'bad-cover.json';path.write_text(json.dumps({'background':str(background),'texts':[layer]}))
            result=self.local_command('cover','--spec',str(path),'--output-dir',str(self.root/'bad'))
            self.assertFalse(result['ok'],result)
            self.assertFalse((self.root/'bad').exists())

    def test_stream_preview_is_available_before_request_finishes(self):
        gate=threading.Event()
        with advanced_server('stream-pause',gate) as (base,calls):
            self.configure(base)
            output=self.root/'live'
            process=subprocess.Popen([sys.executable,str(Path(relay.__file__)),'generate','--config',str(self.config),
                '--prompt','猫','--stream','--output-dir',str(output)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,env=self.env)
            try:
                deadline=time.monotonic()+4
                checkpoint=None
                while time.monotonic()<deadline:
                    manifests=list(output.glob('run-*.json'))
                    if manifests:
                        checkpoint=json.loads(manifests[0].read_text())
                        if checkpoint.get('previews'):break
                    time.sleep(.02)
                self.assertIsNone(process.poll())
                self.assertTrue(checkpoint['previews'])
                self.assertEqual(checkpoint['saved_count'],0)
                self.assertTrue(Path(checkpoint['previews'][0]['path']).is_file())
            finally:
                gate.set()
                stdout,stderr=process.communicate(timeout=8)
            self.assertEqual(process.returncode,0,stdout+stderr)
            self.assertEqual(json.loads(stdout)['saved_count'],1)

    def test_history_import_is_idempotent(self):
        with advanced_server() as (base,calls):
            self.configure(base)
            code,result=self.generate()
            session=self.root/'imported.json'
            for _ in range(2):
                history=self.local_command('history','import','--session',str(session),'--from-run',result['manifest'])
                self.assertTrue(history['ok'],history)
                self.assertEqual(len(history['revisions']),1)
            self.assertEqual(len(calls),1)

    def test_invalid_profiles_and_sessions_are_reported(self):
        with advanced_server() as (base,calls):
            self.configure(base)
            path=self.root/'invalid.json'
            for profile in [{'magic':True},{'stream':'yes'},{'sizes':'1024x1024'}]:
                path.write_text(json.dumps(profile))
                code,result=self.generate('--capabilities',str(path))
                self.assertEqual(code,1,result)
            path.write_text(json.dumps({'version':1,'revisions':[],'selected':3}))
            code,result=self.generate('--session',str(path))
            self.assertEqual(code,1,result)
            self.assertEqual(calls,[])

    def test_studio_access_selection_mask_and_no_generation(self):
        with advanced_server() as (base,calls):
            self.configure(base)
            session=self.root/'session.json'
            code,result=self.generate('--session',str(session))
            self.assertEqual(code,0,result)
            relay.features()  # Initialize runtime module discovery as the CLI does.
            import relay_studio
            server,url=relay_studio.make_server(session,0,relay.api_module())
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                root,query=url.split('/?',1)
                def request(path,body=None):
                    req=urllib.request.Request(root+path+'?'+query,data=json.dumps(body).encode() if body is not None else None,
                        headers={'Content-Type':'application/json'})
                    return urllib.request.urlopen(req)
                with request('/') as response:self.assertIn('涂抹'.encode(),response.read())
                with request('/api/state') as response:self.assertEqual(len(json.load(response)['revisions']),1)
                with request('/api/select',{'revision':1}) as response:self.assertTrue(json.load(response)['ok'])
                data=io.BytesIO();Image.new('RGBA',(1,1),(255,255,255,0)).save(data,format='PNG')
                with request('/api/mask',{'revision':1,'png':base64.b64encode(data.getvalue()).decode()}) as response:
                    mask=json.load(response);self.assertTrue(Path(mask['mask']).is_file())
                with self.assertRaises(urllib.error.HTTPError) as caught:urllib.request.urlopen(root+'/api/state')
                self.assertEqual(caught.exception.code,403)
                caught.exception.close()
                self.assertEqual(len(calls),1)
            finally:server.shutdown();server.server_close();thread.join()

# Avoid inheriting and duplicating the original suite's 34 tests.
for name in baseline.RelayTests.__dict__:
    if name.startswith('test_') and name not in FeatureTests.__dict__:
        setattr(FeatureTests,name,None)

if __name__=='__main__':unittest.main()
