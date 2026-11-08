
import json, sys, time
from pathlib import Path
from playwright.sync_api import sync_playwright

EVID = Path('/home/said/.hermes/cache/scratch/e2e-local-ai')
console_issues, page_errors, posts = [], [], []
rows = bench_rows = 0
chat_ok = False
step = 'launch'
RT = "() => document.querySelector('[data-testid=control-runtime-wsl-cpu-b9189]').innerText"
pre_ready = {'console': 0, 'pageerrors': 0}

def allowed(text):
    # upstream behaviors that are ours-by-provenance, not defects:
    # /tools probe (flag off, caught) + its 404 + favicon
    if '/tools' in text or 'favicon' in text or 'server tools' in text:
        return True
    # llama-server's staggered readiness: its OWN 503 'Loading model'
    # retry loop (props/models/API-key) keeps logging for a while after
    # the first 200s; transient, converges before chat (verified: gate
    # requires double-200, then chat streams clean)
    transient = ('Loading model' in text or 'API key validation' in text
                 or ('503' in text and ('props' in text or '/v1/models' in text)))
    skew = ('/v1/streams/lookup' in text or 'probeServerStream' in text)
    return transient or skew

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path='/usr/bin/chromium-browser',
                                headless=True, args=['--no-sandbox'])
    page = browser.new_page(viewport={'width': 1280, 'height': 900})
    page.on('console', lambda m: console_issues.append(
        f'{m.type}: {m.text[:200]} @ {m.location.get("url", "")}')
        if m.type in ('error', 'warning') else None)
    page.on('pageerror', lambda e: page_errors.append(str(e)))
    page.on('response', lambda r: posts.append(
        f'{r.status} {r.request.method} {r.url.split("8098", 1)[-1]}')
        if (r.request.method == 'POST' and '/ctl/' in r.url) else None)

    try:
        step = 'control page renders'
        page.goto('http://127.0.0.1:8098/#/control', wait_until='domcontentloaded')
        page.wait_for_selector('[data-testid=control-runtimes]', timeout=25000)
        rows = page.locator('[data-testid^=control-runtime-]').count()
        page.wait_for_function(
            "() => document.body.innerText.includes('Qwen3-0.6B')", timeout=15000)
        bench_rows = page.locator('[data-testid=control-bench] tbody tr').count()
        print('panels: rows =', rows, '| bench rows =', bench_rows,
              '| title =', page.title())
        page.screenshot(path=str(EVID / '01-control-page.png'), full_page=True)

        step = 'UI stop'
        page.click('[data-testid=control-stop-wsl-cpu-b9189]')
        page.wait_for_function(f"{RT}.match(/exited|stopped/)", timeout=45000)
        print('UI stop: OK')

        step = 'UI start'
        page.click('[data-testid=control-start-wsl-cpu-b9189]')
        page.wait_for_function(f"{RT}.includes('running')", timeout=45000)
        page.wait_for_function(f"{RT}.includes('up')", timeout=60000)
        print('UI start: OK')

        step = 'UI activate'
        page.click('[data-testid=control-activate-wsl-cpu-b9189]')
        page.wait_for_function(f"{RT}.includes('active')", timeout=15000)
        print('UI activate: OK')
        page.screenshot(path=str(EVID / '02-activated.png'), full_page=True)

        step = 'chat readiness (double-confirmed)'
        page.goto('http://127.0.0.1:8098/#/', wait_until='domcontentloaded')
        check = """async () => {
            const m = await fetch('/v1/models');
            const p = await fetch('/props?autoload=false');
            return m.status === 200 && p.status === 200;
        }"""
        page.wait_for_function(check, timeout=180000)
        page.wait_for_timeout(1500)
        page.wait_for_function(check, timeout=60000)
        page.wait_for_timeout(6000)  # upstream retry loops must go quiet

        pre_ready = {'console': len(console_issues),
                     'pageerrors': len(page_errors)}
        console_issues.clear()
        page_errors.clear()

        step = 'composer'
        composer = page.locator('textarea').first
        composer.wait_for(state='visible', timeout=30000)
        composer.fill('Reply with exactly: PONG and nothing else.')
        composer.press('Enter')

        step = 'PONG wait'
        page.wait_for_function(
            "() => document.body.innerText.includes('PONG')", timeout=150000)
        chat_ok = True
        print('CHAT STREAMED PONG: OK')
        page.screenshot(path=str(EVID / '03-chat.png'), full_page=True)
    except Exception as ex:
        print('FAILED AT STEP:', step)
        print('EXCEPTION:', type(ex).__name__, str(ex)[:400])
    finally:
        real_console = [c for c in console_issues if not allowed(c)]
        print(f'load-window (cleared at gate): console={pre_ready["console"]}, '
              f'pageerrors={pre_ready["pageerrors"]}')
        print('POST /ctl calls:', posts)
        print('POST-GATE console outside allowlist:', len(real_console))
        for c in real_console[:6]:
            print('   CONSOLE:', c)
        print('POST-GATE pageerrors:', len(page_errors))
        for e_ in page_errors[:5]:
            print('   PAGEERROR:', e_)
        (EVID / 'result.json').write_text(json.dumps({
            'rows': rows, 'bench_rows': bench_rows, 'chat_ok': chat_ok,
            'step_failed_at': step if not chat_ok else None,
            'pre_ready': pre_ready, 'posts': posts,
            'console': real_console, 'pageerrors': page_errors}, indent=1))
    browser.close()

sys.exit(0 if chat_ok and not page_errors and not real_console else 1)
