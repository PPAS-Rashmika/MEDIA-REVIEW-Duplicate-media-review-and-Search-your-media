"""Exercise version-three controls in the real Windows embedded browser.

All input files, caches, saved scans and screenshots live below the supplied
parent directory. No user library or connected cloud account is accessed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import threading
import time
import traceback

from PIL import Image, ImageDraw


def _fixtures(parent):
    from audio_fingerprint import ffmpeg_binary

    base = Path(parent) / 'v3-native-ui'
    base.mkdir(parents=True, exist_ok=False)
    first, second = base / 'originals', base / 'copies'
    first.mkdir()
    second.mkdir()
    image = Image.new('RGB', (240, 160), '#203858')
    draw = ImageDraw.Draw(image)
    draw.rectangle((12, 15, 100, 85), fill='#ed8844')
    draw.ellipse((135, 65, 220, 145), fill='#6dad79')
    image.save(first / 'original.png')
    shutil.copy2(first / 'original.png', second / 'copy.png')
    for path in (first / 'original.txt', second / 'copy.txt'):
        path.write_text('Generated Media Review interface fixture only.', encoding='utf-8')
    ffmpeg = ffmpeg_binary()
    assert ffmpeg is not None, 'The video decoder is unavailable.'
    common = [str(ffmpeg), '-hide_banner', '-loglevel', 'error', '-nostdin']
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    subprocess.run(common + ['-f', 'lavfi', '-i', 'testsrc2=size=160x96:rate=8',
                   '-t', '3', '-an', '-c:v', 'libx264', '-crf', '17',
                   '-pix_fmt', 'yuv420p', str(first / 'original.mp4')],
                   capture_output=True, check=True, timeout=30, creationflags=flags)
    subprocess.run(common + ['-i', str(first / 'original.mp4'), '-an', '-vf', 'scale=128:80',
                   '-c:v', 'libx264', '-crf', '23', str(second / 'reencoded.mp4')],
                   capture_output=True, check=True, timeout=30, creationflags=flags)
    return base, first, second


def run(parent, report_path=None):
    """Open, verify and close one native desktop window on the main thread."""
    from app import ReviewApp, create_server
    from launcher import DesktopSession

    base, first, second = _fixtures(parent)
    report_path = Path(report_path) if report_path else base / 'report.json'
    report_path.parent.mkdir(parents=True, exist_ok=True)
    original_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                       for root in (first, second) for p in root.iterdir()}
    backend = ReviewApp(session_directory=base / 'saved-scans')
    session = DesktopSession(first, create_server_func=lambda root, port=0:
                             create_server(root, port, app=backend))
    finished = threading.Event()
    outcome = {'ok': False, 'checks': [], 'generatedFilesOnly': True}

    def write():
        report_path.write_text(json.dumps(outcome, indent=2), encoding='utf-8')

    def check(name, condition, details=None):
        if not condition:
            raise AssertionError(f'{name}: {details!r}')
        outcome['checks'].append(name)

    def js(code):
        return session.window.run_js(code)

    def wait(predicate, message, seconds=30):
        until = time.monotonic() + seconds
        while time.monotonic() < until:
            value = predicate()
            if value:
                return value
            time.sleep(0.1)
        raise AssertionError(message)

    def click(identifier):
        js(f"document.getElementById({json.dumps(identifier)}).click()")

    def ui_idle():
        return js("!document.getElementById('scan-button').disabled && document.getElementById('error-banner').hidden")

    def capture(name):
        # Capture WebView2 itself; no desktop or unrelated application is read.
        from System import Action
        from System.IO import MemoryStream
        from Microsoft.Web.WebView2.Core import CoreWebView2CapturePreviewImageFormat

        stream = MemoryStream()
        tasks = []
        session.window.native.Invoke(Action(lambda: tasks.append(
            session.window.native.browser.webview.CoreWebView2.CapturePreviewAsync(
                CoreWebView2CapturePreviewImageFormat.Png, stream))))
        wait(lambda: tasks[0].IsCompleted, 'The embedded browser screenshot timed out.', 15)
        if tasks[0].IsFaulted:
            raise RuntimeError(str(tasks[0].Exception))
        target = base / name
        target.write_bytes(bytes(stream.ToArray()))
        stream.Dispose()
        return str(target)

    def loaded():
        try:
            ready = wait(lambda: js("({ready:!!document.getElementById('compare-images'), error:!document.getElementById('error-banner')?.hidden, title:document.title})"),
                         'The native document did not become available.')
            wait(lambda: js("!!document.getElementById('compare-images') && !!document.getElementById('tools-confirm') && document.getElementById('error-banner').hidden"),
                 f'The application modules did not initialize: {ready}', 30)
            js("window.__v3RuntimeErrors=[];window.addEventListener('error',e=>window.__v3RuntimeErrors.push(e.message));window.addEventListener('unhandledrejection',e=>window.__v3RuntimeErrors.push(String(e.reason)))")
            modules = js("['Advanced','Cloud','Workspace','FileTools'].map(name=>!!window['MediaReview'+name])")
            check('All four extension modules initialize', all(modules), modules)
            state = backend.state()
            check('Startup remains blank and requires Scan', not state['hasScanned'] and not state['scanning']
                  and state['root'] == '' and state['stats']['total'] == 0, state)
            check('One WebView2 window', session.window.gui.renderer == 'edgechromium'
                  and len(session.webview.windows) == 1)
            outcome['screenshots'] = {'startup': capture('startup.png')}
            click('tools-nav')
            check('File tools navigation works before scan', js("!document.getElementById('tools-view').hidden && document.getElementById('page-title').textContent==='File tools'"))
            check('File mutations disabled before scan', js("Array.from(document.querySelectorAll('#tools-view [data-tool]')).every(b=>b.disabled) && document.getElementById('tools-report').disabled"))
            click('review-nav')
            js(f"document.getElementById('folder-path').value={json.dumps(str(first))};document.getElementById('folder-path').dispatchEvent(new Event('input',{{bubbles:true}}));document.getElementById('workspace-setup').open=true")
            click('add-scan-root')
            js(f"document.querySelector('.extra-root-path').value={json.dumps(str(second))};document.getElementById('primary-root-role').value='reference'")
            roots = js('window.MediaReviewWorkspace.readRoots()')
            check('Multiple roots and reference protection collected', len(roots) == 2 and roots[0]['reference']
                  and roots[0]['protected'] and roots[1]['path'] == str(second), roots)
            time.sleep(0.3)
            check('Choosing roots never starts scanning', not backend.state()['hasScanned'])
            click('options-button')
            js("document.getElementById('filter-extensions').value='png, mp4, txt';document.getElementById('filter-exclude').value='Excluded/**';document.getElementById('filter-min-size').value='0.000001';for(const id of ['match-transforms','match-crop','match-video','match-audio-tags','match-folders'])document.getElementById(id).checked=true;document.getElementById('comparison-scope').value='reference'")
            options = js('({...window.MediaReviewAdvanced.readOptions(),...window.MediaReviewWorkspace.readOptions()})')
            check('Advanced and library matching settings collected together', options['extensions'] == ['png', 'mp4', 'txt']
                  and options['videoMatching'] and options['imageTransforms'] and options['cropMatching']
                  and options['audioTagMatching'] and options['folderMatching'] and options['scanCache']
                  and options['comparisonScope'] == 'reference', options)
            click('apply-advanced')
            click('scan-button')
            wait(lambda: backend.state()['hasScanned'], 'The explicit Scan button did not start a scan.')
            wait(lambda: not backend.state()['scanning'], 'The generated multi-folder scan timed out.', 120)
            wait(ui_idle, 'The UI did not unlock after the multi-folder scan.')
            state = backend.state()
            check('UI scan sends both roots and all matching options', len(state['roots']) == 2
                  and state['scanOptions']['videoMatching'] and state['scanOptions']['comparisonScope'] == 'reference', state['scanOptions'])
            check('Generated multi-folder files are indexed', state['stats']['total'] == 6, state['stats'])
            check('Similar video results reach the UI', any(g['kind'] == 'video' for g in state['groups']), state['groups'])
            image_group = next(g for g in state['groups'] if all(backend.result.photos[p].mediaType == 'image' for p in g['photos']))
            js(f"document.querySelector('[data-group={json.dumps(image_group['id'])}]').click()")
            wait(lambda: js("!document.getElementById('compare-images').disabled"), 'Linked image comparison was unavailable for a valid image pair.')
            click('compare-images')
            check('Linked image comparison opens two real images', js("document.getElementById('linked-images-dialog').open && document.querySelectorAll('.linked-image-pane img').length===2"))
            js("document.getElementById('linked-zoom').value='200';document.getElementById('linked-zoom').dispatchEvent(new Event('input',{bubbles:true}))")
            check('Linked zoom updates both images', js("Array.from(document.querySelectorAll('.linked-image-pane img')).every(image=>image.style.width==='200%')"))
            wait(lambda: js("Array.from(document.querySelectorAll('.linked-image-pane img')).every(image=>image.complete && image.naturalWidth>0)"),
                 'The generated full-size images did not load.')
            js("document.querySelector('.linked-image-pane').scrollLeft=120;document.querySelector('.linked-image-pane').dispatchEvent(new Event('scroll'))")
            wait(lambda: js("document.querySelectorAll('.linked-image-pane')[1].scrollLeft>0"),
                 'Scrolling the first image did not pan the second image.')
            check('Image panning is linked across both panes', js("Math.abs(document.querySelectorAll('.linked-image-pane')[0].scrollLeft-document.querySelectorAll('.linked-image-pane')[1].scrollLeft)<3"))
            outcome['screenshots']['linkedImages'] = capture('linked-images.png')
            click('close-linked-images')
            video_group = next(g for g in state['groups'] if g['kind'] == 'video')
            js(f"document.querySelector('[data-group={json.dumps(video_group['id'])}]').click()")
            check('Video timeline has two sampled rows', js("!document.getElementById('synchronized-timeline').hidden && document.querySelectorAll('.timeline-row').length===2 && Array.from(document.querySelectorAll('.timeline-row')).every(row=>row.querySelectorAll('button').length>=4)"))
            js("document.getElementById('timeline-position').value='50';document.getElementById('timeline-position').dispatchEvent(new Event('input',{bubbles:true}))")
            check('Synchronized timeline selects corresponding frames', js("document.querySelectorAll('.timeline-selected').length===2 && Array.from(document.querySelectorAll('.timeline-selected')).every(button=>Number(button.dataset.timelineIndex)>0)"))
            js("document.getElementById('synchronized-timeline').scrollIntoView({block:'center'})")
            outcome['screenshots']['videoTimeline'] = capture('video-timeline.png')
            click('tools-nav')
            wait(lambda: js("!document.getElementById('tools-apply-keepers').disabled"), 'File tools stayed locked after scanning.')
            js("document.getElementById('tools-keep-rule').value='oldest'")
            click('tools-apply-keepers')
            wait(lambda: js("document.getElementById('tools-keepers-status').textContent.includes('Keep rule applied')"), 'Keep-rule UI did not complete.')
            check('Keep rules select protected originals', bool(backend.keeper_choices)
                  and all(backend.result.photos[p].protected for p in backend.keeper_choices.values()), backend.keeper_choices)
            js("document.querySelector('[data-tool=rename]').click()")
            wait(lambda: js("document.getElementById('tools-plan-dialog').open"), 'The batch rename preview did not open.')
            check('Batch rename previews only manageable copies', js("document.querySelectorAll('#tools-plan-list article').length===3"))
            click('tools-cancel')
            check('Cancelled preview does not move or rename files', all(Path(p).is_file() for p in original_hashes))
            outcome['screenshots']['fileTools'] = capture('file-tools.png')
            click('review-nav')
            # A completed generated scan can stand in for work committed before
            # interruption. The UI must find it, explicitly resume, and reuse it.
            backend.interrupted_store.write(state['roots'], {'recursive': True, 'threshold': 94,
                'includeAllFiles': True, 'options': state['scanOptions'], 'catalogIds': []})
            click('check-interrupted')
            wait(lambda: js("!document.getElementById('interrupted-scan').hidden"), 'Unfinished-scan recovery did not appear.')
            check('Recovery requires an explicit Resume action', not backend.state()['scanning'])
            click('resume-interrupted')
            wait(lambda: backend.state()['scanning'] or backend.state()['stats'].get('cacheHits', 0) == 6,
                 'The recovery button did not resume the generated scan.')
            wait(lambda: not backend.state()['scanning'], 'Resumed scan did not finish.', 120)
            wait(ui_idle, 'The UI stayed locked after recovery.')
            check('Recovery reuses completed analyses', backend.state()['stats'].get('cacheHits') == 6, backend.state()['stats'])
            click('sessions-nav')
            wait(lambda: js("!document.getElementById('save-session').disabled"), 'Save scan stayed locked.')
            js("document.getElementById('session-name').value='Generated native UI scan';document.getElementById('save-session-form').requestSubmit()")
            wait(lambda: js("document.querySelectorAll('[data-open-catalog]').length===1"), 'The saved scan did not expose an offline catalog control.')
            check('Saved scan offers Open scan and Open catalog', js("document.querySelectorAll('[data-load-session]').length===1 && document.querySelectorAll('[data-open-catalog]').length===1"))
            click('review-nav')
            click('refresh-catalogs')
            wait(lambda: js("document.querySelectorAll('#catalog-choices input').length===1"), 'The catalog picker did not load saved scans.')
            js("document.querySelector('#catalog-choices input').checked=true")
            check('Selected catalogs are collected for the next explicit scan', len(js('window.MediaReviewWorkspace.catalogIds()')) == 1)
            click('sessions-nav')
            wait(lambda: js("document.querySelectorAll('[data-open-catalog]').length===1"), 'The catalog action disappeared.')
            js("document.querySelector('[data-open-catalog]').click()")
            wait(lambda: backend.state().get('catalogOnly'), 'Open catalog did not load a read-only snapshot.')
            wait(lambda: js("!document.getElementById('catalog-mode-note').hidden"), 'The catalog mode explanation was not displayed.')
            check('Offline catalogs never start a new scan', not backend.state()['scanning'])
            check('Offline catalog files are read-only', all(p.get('offline') for p in backend.state()['photos']))
            check('No JavaScript errors during native interactions', not js('window.__v3RuntimeErrors'), js('window.__v3RuntimeErrors'))
            check('No interface error banner', js("document.getElementById('error-banner').hidden"), js("document.getElementById('error-text').textContent"))
            check('Generated input files remain byte-for-byte unchanged', original_hashes == {
                p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in original_hashes})
            outcome.update(ok=True, renderer=session.window.gui.renderer, windows=1,
                           indexedFiles=6, liveCloudAccountsTested=False)
        except Exception:
            outcome['error'] = traceback.format_exc()
            try:
                outcome['dom'] = js("({title:document.title,body:document.body.innerText.slice(0,18000),errors:window.__v3RuntimeErrors})")
            except Exception:
                pass
        finally:
            write()
            finished.set()
            session.window.destroy()

    def timeout():
        if not finished.wait(300):
            outcome.update(ok=False, error='The generated native UI checks exceeded five minutes.')
            write()
            if session.window:
                session.window.destroy()

    threading.Thread(target=timeout, daemon=True, name='v3-native-check-timeout').start()
    session.run(loaded_callback=loaded)
    outcome['serverClosed'] = session.server.fileno() == -1
    write()
    if not outcome['ok']:
        raise RuntimeError(outcome.get('error', 'The native UI check closed before completing.'))
    return outcome


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent', required=True, type=Path)
    parser.add_argument('--report', type=Path)
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.parent, arguments.report)))
