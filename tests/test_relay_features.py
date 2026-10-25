"""Observable offline protocol, revision, model delegation and UI behaviors."""
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
                    name=data.get('type','')
                    if mode=='stream-header':
                        data=dict(data)
                        data.pop('type',None)
                    raw = json.dumps(data, ensure_ascii=False).encode()
                    self.wfile.write(b': keepalive\r\n\r\nevent: '+name.encode()+b'\r\ndata: '+raw+b'\r\n\r\n')
                    self.wfile.flush()
                prefix = 'image_edit' if self.path.endswith('/edits') else 'image_generation'
                event({'type': prefix+'.partial_image', 'partial_image_b64': ENCODED, 'b64_json': ENCODED,'partial_image_index':0})
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
                payload={'data':[image],'usage':{'output_tokens':5}}
                if mode=='metadata':
                    payload.update(created=123456,background='opaque',output_format='png',quality='max',size='1x1',safety_identifier='anon-test')
                self.wfile.write(json.dumps(payload).encode())
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

    def test_official_json_url_file_id_and_mask_references(self):
        references=[{'image_url':'https://example.com/reference.png?version=1'},{'file_id':'file-input'}]
        path=self.root/'references.json'
        path.write_text(json.dumps(references))
        with advanced_server() as (base,calls):
            self.configure(base)
            code,result=self.edit('--images-file',str(path),'--mask-file-id','file-mask','--safety-identifier','anonymous-test','--user','legacy-test')
            self.assertEqual(code,0,result)
            self.assertEqual(calls[0]['path'],'/v1/images/edits')
            self.assertEqual(calls[0]['body']['images'],references)
            self.assertEqual(calls[0]['body']['mask'],{'file_id':'file-mask'})
            self.assertEqual(calls[0]['body']['safety_identifier'],'anonymous-test')
            self.assertEqual(calls[0]['body']['user'],'legacy-test')
            self.assertEqual(result['references'],references)
            self.assertEqual(result['mask'],{'file_id':'file-mask'})
            self.assertEqual(result['edit_transport'],'json')

    def test_explicit_remote_references_do_not_silently_include_selected_version(self):
        with advanced_server() as (base,calls):
            self.configure(base)
            session=self.root/'session.json'
            self.assertEqual(self.generate('--session',str(session))[0],0)
            data_url='data:image/png;base64,'+ENCODED
            code,result=self.edit('--session',str(session),'--image-url',data_url,'--image-file-id','file-input','--mask-url','https://example.com/mask.png')
            self.assertEqual(code,0,result)
            self.assertEqual(calls[1]['body']['images'],[{'image_url':data_url},{'file_id':'file-input'}])
            self.assertEqual(calls[1]['body']['mask'],{'image_url':'https://example.com/mask.png'})
            self.assertIsNone(result['parent'])
            self.assertNotIn(data_url,Path(result['manifest']).read_text())

    def test_remote_reference_validation_happens_before_network(self):
        with advanced_server() as (base,calls):
            self.configure(base)
            path=self.root/'references.json'
            for values in [[],[{}],[{'image_url':'https://example.com/a.png','file_id':'file-a'}],[{'image_url':'file:///tmp/local.png'}],[{'file_id':''}],[{'file_id':'file-a'}]*17]:
                path.write_text(json.dumps(values))
                self.assertEqual(self.edit('--images-file',str(path))[0],1)
            self.assertEqual(self.edit('--image-url','https://example.com/a.png','--edit-transport','multipart')[0],1)
            self.assertEqual(calls,[])

    def test_safety_identifier_and_gpt_only_boundary(self):
        with advanced_server() as (base,calls):
            self.configure(base)
            code,result=self.generate('--safety-identifier','anon-123','--user','legacy-id','--quality','max','--size','1536x864')
            self.assertEqual(code,0,result)
            self.assertEqual(calls[0]['body']['safety_identifier'],'anon-123')
            self.assertEqual(calls[0]['body']['quality'],'max')
            self.assertEqual(self.generate('--safety-identifier','a'*129)[0],1)
            self.assertEqual(len(calls),1)
        for args in [('--backend','responses'),('--response-format','url')]:
            proc=subprocess.run([sys.executable,str(Path(relay.__file__)),'generate','--prompt','test',*args],capture_output=True,text=True,env=self.env,timeout=10)
            self.assertEqual(proc.returncode,2)

    def test_declared_output_format_mismatch_is_reported_without_transcoding(self):
        with advanced_server() as (base,calls):
            self.configure(base)
            code,result=self.generate('--output-format','webp')
            self.assertEqual(code,0,result)
            self.assertEqual(Path(result['files'][0]).suffix,'.png')
            self.assertEqual(Path(result['files'][0]).read_bytes(),baseline.PNG)
            self.assertTrue(any('请求格式 webp' in item['warning'] for item in result['warnings']))

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
            for options in [('--partial-images','2'),('--output-compression','101'),('--output-compression','40')]:
                code,result=self.generate(*options)
                self.assertEqual(code,1,result)
            self.assertEqual(calls,[])

    def test_capability_profile_rejects_only_in_explicit_strict_mode(self):
        with advanced_server() as (base,calls):
            self.configure(base)
            profile=self.root/'profile.json'
            profile.write_text(json.dumps({'stream':False,'background':['opaque'],'max_n':1,'backends':['images']}))
            code,result=self.cli('capabilities','set','--file',str(profile))
            self.assertEqual(code,0,result)
            for opts in [('--stream',),('--background','transparent'),('--n','2')]:
                code,result=self.generate('--strict-capabilities',*opts)
                self.assertEqual(code,1,result)
            self.assertEqual(calls,[])
            code,result=self.generate('--n','2','--request-mode','separate','--strict-capabilities')
            self.assertEqual(code,0,result)
            self.assertEqual(len(calls),2)

    def test_stale_capability_profile_is_advisory_by_default(self):
        with baseline.server('multi') as (base,calls):
            self.configure(base)
            profile=self.root/'profile.json'
            profile.write_text(json.dumps({'background':['opaque'],'max_n':1}))
            self.assertEqual(self.cli('capabilities','set','--file',str(profile))[0],0)
            code,result=self.generate('--background','transparent','--n','2')
            self.assertEqual(code,0,result)
            self.assertEqual(len(calls),1)
            self.assertEqual(calls[0][3]['n'],2)
            self.assertEqual(calls[0][3]['background'],'transparent')
            self.assertEqual(len(result['warnings']),2)
            manifest=json.loads(Path(result['manifest']).read_text())
            self.assertEqual(manifest['warnings'],result['warnings'])

    def test_edit_is_not_blocked_by_stale_profile(self):
        with advanced_server() as (base,calls):
            self.configure(base)
            profile=self.root/'profile.json'
            profile.write_text(json.dumps({'edit':False,'max_reference_images':1}))
            code,result=self.edit('--image',str(self.image()),'--image',str(self.image('second.png')),'--capabilities',str(profile))
            self.assertEqual(code,0,result)
            self.assertEqual(calls[0]['path'],'/v1/images/edits')
            self.assertEqual(len(result['warnings']),2)

    def test_extra_parameters_reach_images_edits_unchanged(self):
        parameters={'provider_feature':{'enabled':True,'labels':['原样',2]},'provider_seed':123}
        path=self.root/'params.json'
        path.write_text(json.dumps(parameters,ensure_ascii=False))
        for backend,transport in [('images','multipart'),('images','json')]:
            with self.subTest(backend=backend,transport=transport),advanced_server(backend) as (base,calls):
                self.configure(base)
                source=self.image()
                original=source.read_bytes()
                args=['--image',str(source),'--api-params-file',str(path),'--edit-transport',transport]
                code,result=self.edit(*args)
                self.assertEqual(code,0,result)
                body=calls[0]['body']
                sent=body
                for key,value in parameters.items():
                    if backend=='images' and transport=='multipart':
                        self.assertEqual(json.loads(sent[key]),value)
                    else:
                        self.assertEqual(sent[key],value)
                self.assertEqual(Path(result['files'][0]).read_bytes(),baseline.PNG)
                self.assertEqual(source.read_bytes(),original)

    def test_extra_parameters_cannot_silently_override_request_or_use_nonfinite_numbers(self):
        with advanced_server() as (base,calls):
            self.configure(base)
            for params in [{'prompt':'changed'},{'n':20},{'image':'changed'},{'image[]':'changed'},{'size':'changed'},
                           {'feature':float('nan')},{'feature':float('inf')},{'field\r\ninjected':'bad'},{'field"':'bad'},[]]:
                path=self.root/'params.json'
                path.write_text(json.dumps(params))
                code,result=self.generate('--api-params-file',str(path))
                self.assertEqual(code,1,result)
            self.assertEqual(calls,[])

    def test_standard_option_values_are_not_frozen_to_old_enums(self):
        with advanced_server() as (base,calls):
            self.configure(base)
            code,result=self.edit('--image',str(self.image()),'--input-fidelity','provider-auto','--moderation','provider-default','--background','provider-new')
            self.assertEqual(code,0,result)
            self.assertEqual(calls[0]['body']['input_fidelity'],'provider-auto')
            self.assertEqual(calls[0]['body']['moderation'],'provider-default')
            self.assertEqual(calls[0]['body']['background'],'provider-new')

    def test_images_stream_previews_and_final(self):
        with advanced_server('stream') as (base,calls):
            self.configure(base)
            code,result=self.generate('--stream','--partial-images','2')
            self.assertEqual(code,0,result)
            self.assertEqual(len(result['previews']),1)
            self.assertEqual(result['saved_count'],1)
            self.assertIs(calls[0]['body']['stream'],True)
            self.assertEqual(result['requests'][0]['event_usage'],[{'output_tokens':5}])

    def test_official_response_metadata_and_stream_preview_index_are_retained(self):
        with advanced_server('metadata') as (base,calls):
            self.configure(base)
            code,result=self.generate()
            self.assertEqual(code,0,result)
            metadata=result['requests'][0]['response_metadata']
            self.assertEqual(metadata['created'],123456)
            self.assertEqual(metadata['quality'],'max')
            self.assertEqual(metadata['safety_identifier'],'anon-test')
            self.assertNotIn('data',metadata)
        with advanced_server('stream-header') as (base,calls):
            self.configure(base)
            code,result=self.edit('--image',str(self.image()),'--stream','--partial-images','1')
            self.assertEqual(code,0,result)
            self.assertEqual(result['previews'][0]['response_metadata']['partial_image_index'],0)
            self.assertEqual(result['images'][0]['usage']['output_tokens'],5)
            self.assertEqual(len(calls),1)

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

    def test_local_cover_renderer_is_no_longer_an_executable_command(self):
        output=self.root/'covers'
        proc=subprocess.run([sys.executable,str(Path(relay.__file__)),'cover','--spec','unused.json','--output-dir',str(output)],
                            capture_output=True,text=True,env=self.env,timeout=10)
        self.assertEqual(proc.returncode,2)
        self.assertFalse(output.exists())
        self.assertFalse(Path(relay.__file__).with_name('relay_cover.py').exists())

    def local_command(self,*args):
        proc=subprocess.run([sys.executable,str(Path(relay.__file__)),*args],capture_output=True,text=True,env=self.env,timeout=15)
        self.assertEqual(proc.stderr,'',proc.stderr)
        return json.loads(proc.stdout)

    def test_chinese_title_edit_transmits_exact_prompt_and_original_pixels(self):
        prompt='只把标题改为“周末计划·第２期”。\n保持原来的文字位置，不增加任何其他文案。'
        with advanced_server() as (base,calls):
            self.configure(base)
            source=self.image()
            original=source.read_bytes()
            code,result=self.cli('edit','--image',str(source),'--prompt',prompt,'--output-dir',str(self.root/'out'))
            self.assertEqual(code,0,result)
            self.assertEqual(len(calls),1)
            body=calls[0]['body']
            self.assertEqual(calls[0]['path'],'/v1/images/edits')
            self.assertEqual(body['prompt'],prompt)
            self.assertEqual(body['image'],original)
            self.assertEqual(Path(result['files'][0]).read_bytes(),baseline.PNG)
            for option in ['size','quality','input_fidelity','moderation','output_format','background']:
                self.assertNotIn(option,body)

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
