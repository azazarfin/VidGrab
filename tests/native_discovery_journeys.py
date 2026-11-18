"""P05 journeys through installed content scripts and the actual native facade."""

from pathlib import Path
import json

from tests.native_client_support import wait


def discovery_journey(browser, native, base, fixture, screenshots, *, grants=False):
    source = browser.open('popup/popup.html')
    document_url = base + '/discovery/watch?browser=' + browser.kind
    source.goto(document_url)
    tab = wait(lambda: browser.background('''return new Promise(resolve =>
        this.chrome.tabs.query({}, tabs => resolve(tabs.find(tab => tab.url === arguments[0]))));''',
        document_url), 'installed source tab identity')
    tab_id = tab['id']
    key = 'detected_videos_' + str(tab_id)

    def candidates():
        return browser.background('''return new Promise(resolve =>
            this.chrome.storage.local.get(arguments[0], value => resolve(value[arguments[0]] || [])));''', key)

    detected = wait(lambda: (items if len(items := candidates()) == 3 else None),
        'actual document, audio source and nested frame detection')
    signed_path = '/' + browser.kind + '-discovery-referer.wav'
    signed = base + signed_path + '?sig=a%2Fb+Z&part=2&part=1'
    selected = next(item for item in detected if item['url'] == signed)
    nested = next(item for item in detected if item['url'] == base + '/discovery-frame.wav')
    assert selected['page_url'] == document_url
    assert nested['page_url'] == selected['page_url']
    assert nested['frame_url'] == base + '/discovery/frame'
    assert nested['browser_context']['frame_id'] != selected['browser_context']['frame_id']
    assert all(item['browser_context']['document_token'] for item in detected)
    assert any(item['kind'] == 'audio' and item['url'] == base + '/discovery-audio.wav' for item in detected)
    jobs_before = len(native.jobs())
    assert native.job(signed) is None

    browser.background('''const identity = arguments[0], scope = this;
        scope.__discoveryHandoff = null;
        scope.__discoveryHandoffObserver = (changes, area) => {
            const selected = changes.pendingVideoCandidate?.newValue;
            if (area === 'local' && selected?.id === identity) scope.__discoveryHandoff = selected;
        };
        this.chrome.storage.onChanged.addListener(scope.__discoveryHandoffObserver);
        return true;''', selected['id'])

    source.script("document.querySelector('#video').dispatchEvent(new MouseEvent('mouseenter',{bubbles:false}));")
    source.click('#vidgrab-overlay-btn')
    pending = wait(lambda: browser.background('return this.__discoveryHandoff'), 'actual overlay handoff event')
    browser.background('''this.chrome.storage.onChanged.removeListener(this.__discoveryHandoffObserver);
        delete this.__discoveryHandoffObserver; return true;''')
    assert pending['id'] == selected['id'] and pending['url'] == signed
    assert len(native.jobs()) == jobs_before, 'Overlay selection enqueued media'
    source.screenshot(screenshots / (browser.kind + '-discovery-source.png'))

    # The real action popup probes its handoff on startup. Join that returned
    # read before the diagnostic facade sequence prepares another context.
    def handoff_response():
        state = browser.state()
        return state if any(item['operation'] == 'probes.create' for item in state['trace']) else None

    # WebDriver switches Firefox to the diagnostic extension tab, closing its
    # transient action popup. Chrome's CDP background evaluation keeps it open.
    handoff = wait(handoff_response, 'actual popup handoff probe response') if browser.kind == 'chrome' else browser.state()
    handoff_counts = {operation: sum(item['operation'] == operation for item in handoff['trace'])
        for operation in ('auth.create', 'probes.create', 'downloads.enqueue')}

    result = browser.background('''return (async () => {
        const [tabId, candidate] = arguments;
        await this.chrome.tabs.update(tabId, {active:true});
        const api = this.vidGrabAPI;
        const permission = await api.getPermissions(candidate.url);
        if (permission.cookie_forwarding_enabled) throw new Error('Select the initial header-only fixture');
        const context = await api.prepareAuth(candidate.url, null, false, candidate.id);
        const wire = [], transport = this.vidGrabNative, original = transport.request;
        transport.request = async request => {
            if (['probes.create','downloads.enqueue'].includes(request.operation))
                wire.push({operation:request.operation, reference:request.params.auth_ref || null});
            return original.call(transport, request);
        };
        let probe, reused, job;
        try {
            probe = await api.probeUrl(candidate.url, {candidate_id:candidate.id});
            reused = await api.prepareAuth(candidate.url, null, false, candidate.id);
            job = await api.startDownload(candidate.url, null, null, null, {}, {candidate_id:candidate.id});
        } finally { transport.request = original; }
        const probes = wire.filter(item => item.operation === 'probes.create');
        const queued = wire.find(item => item.operation === 'downloads.enqueue');
        return {context_created:Boolean(context.reference), same_context:context.reference === reused.reference,
            wire_same_context:Boolean(queued?.reference) && probes.every(item => item.reference === queued.reference),
            probe_requests:probes.length, context_instance_matches:context.instance_id === api.session.state.capabilities.instance_id,
            context_lifetime:context.expires_at - Date.now()/1000,
            probe_witness:Boolean(probe.probe_id), formats:probe.formats.length, job_id:job.job_id};
    })();''', tab_id, selected)
    (screenshots / (browser.kind + '-discovery-context.json')).write_text(
        json.dumps(result, indent=2) + '\n', encoding='utf-8')
    assert result['context_created'] and result['same_context'] and result['probe_witness'], result
    assert result['probe_requests'] > 0 and result['wire_same_context'], result
    complete = wait(lambda: native.job(signed, 'complete'), 'header-only native fixture completion', 40)
    assert complete['id'] == result['job_id'] and Path(complete['file_path']).is_file()
    assert any(request['path'] == signed_path and request['origin_referer']
        and request['user_agent'] and not request['authenticated'] for request in fixture['requests'])

    source.script("document.querySelector('#frame').src='/discovery/next';")
    wait(lambda: all(item['id'] != nested['id'] for item in candidates()),
        'nested document navigation invalidates the old candidate')
    rejected = browser.background('''return (async () => {
        await this.chrome.tabs.update(arguments[0], {active:true});
        try { await this.vidGrabAPI.prepareAuth(arguments[1].url, null, false, arguments[1].id); }
        catch (error) { return error.code; }
        throw new Error('An old frame context was accepted');
    })();''', tab_id, nested)
    assert rejected == 'BROWSER_CONTEXT_EXPIRED'
    browser.background('return this.vidGrabAPI.removeCandidates(arguments[0], [arguments[1]])', tab_id, selected['id'])
    source.script("document.querySelector('#video').dispatchEvent(new Event('loadedmetadata',{bubbles:true}));")
    wait(lambda: all(item['url'] != signed for item in candidates()), 'owned dismissal remains in the current document')
    assert browser.background('''return new Promise(resolve => this.chrome.storage.local.get(
        'pendingVideoCandidate', value => resolve(value.pendingVideoCandidate || null)));''') is None

    permission = browser.permissions(base)
    assert permission['request_observation_enabled'] is False
    settings_before = native.settings()['values']
    # Seed only the temporary browser preference. The actual browser grants
    # stay unchanged, so a preference alone cannot install an observer.
    browser.background('''return new Promise(resolve => this.chrome.storage.local.set(
        {requestObservationEnabled:true}, resolve));''')
    wait(lambda: browser.permissions(base)['request_observation_enabled'], 'actual observation preference')
    assert browser.permissions(base)['request_observation_granted'] == permission['request_observation_granted']
    browser.background('return this.vidGrabObservation.configure()')
    source.asynchronous('''fetch('/discovery/manifest.m3u8').then(response => response.text())
        .then(() => arguments[arguments.length-1](true));''')
    source.script("document.querySelector('#audio').src='/discovery-updated.wav';")
    wait(lambda: any(item['url'] == base + '/discovery-updated.wav' for item in candidates()),
        'actual SPA media source mutation')
    if not permission['request_observation_granted']:
        assert all(item['url'] != base + '/discovery/manifest.m3u8' for item in candidates())
    browser.background('''return new Promise(resolve => this.chrome.storage.local.set(
        {requestObservationEnabled:false}, resolve));''')
    browser.background('return this.vidGrabObservation.configure()')
    assert native.settings()['values'] == settings_before

    source.goto(base + '/discovery/next')
    wait(lambda: not candidates(), 'top document navigation clears scoped detection')
    rejected = browser.background('''return (async () => {
        await this.chrome.tabs.update(arguments[0], {active:true});
        try { await this.vidGrabAPI.prepareAuth(arguments[1].url, null, false, arguments[1].id); }
        catch (error) { return error.code; }
        throw new Error('An old document context was accepted');
    })();''', tab_id, selected)
    assert rejected == 'BROWSER_CONTEXT_EXPIRED'
    grant_checks = optional_grants_journey(browser, native, source, base, candidates, screenshots) if grants else None
    return {'browser': browser.kind, 'source_tab_id': tab_id, 'header_only_job_id': complete['id'],
        'same_probe_context': result['same_context'], 'context_checks': result,
        'handoff_native_operations': handoff_counts,
        'request_permissions': permission,
        'optional_grants': grant_checks,
        'checks': ['document/audio/source/nested-frame scope', 'literal signed URL',
            'overlay selection without enqueue', 'header-only native probe/enqueue and origin Referer',
            'nested navigation rejects stale context', 'owned dismissal and pending invalidation',
            'actual optional grants distinct from preference', 'SPA source mutation',
            'top navigation rejects stale context']}


def optional_grants_journey(browser, native, source, base, candidates, screenshots):
    options = browser.options
    settings = native.settings()['values']
    initial = browser.permissions(base)
    assert not initial['request_observation_granted'] and not initial['request_observation_enabled']
    report = {'initial': initial, 'checks': []}
    report_path = screenshots / (browser.kind + '-optional-grants.json')

    def record():
        report_path.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')

    def fetch_manifest(tag):
        source.asynchronous('''fetch(arguments[0], {cache:'no-store'}).then(response => response.text())
            .then(() => arguments[arguments.length-1](true));''', base + '/discovery/manifest.m3u8?cycle=' + tag)

    def request_candidates():
        return [item for item in candidates() if item['source'] == 'request']

    try:
        options.click('[data-tab="tab-advanced"]')
        options.click('#chk-request-observation + .slider')
        wait(lambda: not options.script("return document.querySelector('#chk-request-observation').disabled"),
            'actual browser permission request finishes')
        granted = browser.permissions(base)
        report['after_control_request'] = granted
        report['control_status'] = options.text('#status-message')
        record()
        assert granted['request_observation_granted'] and granted['navigation_granted'], report
        assert granted['request_observation_enabled'], report
        browser.background('return this.vidGrabObservation.configure()')
        source.goto(base + '/discovery/watch?browser=' + browser.kind)
        wait(lambda: len(candidates()) == 3, 'content detection after the actual permission request')
        fetch_manifest('enabled')
        observed = wait(lambda: request_candidates(), 'actual granted response-header observation')
        assert any(item['url'] == base + '/discovery/manifest.m3u8?cycle=enabled'
            and item['frame_url'] == base + '/discovery/watch?browser=' + browser.kind
            and item['browser_context']['frame_id'] == 0 for item in observed), observed
        report['checks'].append('actual control grants and request observation')
        options.screenshot(screenshots / (browser.kind + '-optional-granted.png'))
        options.click('#chk-request-observation + .slider')
        wait(lambda: not browser.permissions(base)['request_observation_enabled'], 'actual observer preference disabled')
        browser.background('return this.vidGrabObservation.configure()')
        wait(lambda: not request_candidates(), 'disabled observer clears its own candidates')
        fetch_manifest('disabled')
        assert not request_candidates()
        assert browser.permissions(base)['request_observation_granted']
        report['checks'].append('grants remain distinct from the disabled preference')
        options.click('#chk-request-observation + .slider')
        wait(lambda: browser.permissions(base)['request_observation_enabled'], 'observer enabled with existing grants')
        removed = browser.background('''return new Promise(resolve => this.chrome.permissions.remove(
            {permissions:['webRequest','webNavigation']}, resolve));''')
        assert removed is True
        revoked = wait(lambda: (value if not (value := browser.permissions(base))['request_observation_granted'] else None),
            'actual optional permissions revoked')
        assert revoked['request_observation_enabled'] and not revoked['navigation_granted']
        browser.background('return this.vidGrabObservation.configure()')
        fetch_manifest('revoked')
        assert not request_candidates()
        report['after_revoke'] = revoked
        report['checks'].append('actual grant removal disables observation while the preference stays enabled')
        assert native.settings()['values'] == settings
        report['result'] = 'passed'
    finally:
        permission = browser.permissions(base)
        if permission['request_observation_granted'] or permission['navigation_granted']:
            browser.background('''return new Promise(resolve => this.chrome.permissions.remove(
                {permissions:['webRequest','webNavigation']}, resolve));''')
        browser.background('''return new Promise(resolve => this.chrome.storage.local.set(
            {requestObservationEnabled:false}, resolve));''')
        browser.background('return this.vidGrabObservation.configure()')
        report['final'] = browser.permissions(base)
        record()
    return report
