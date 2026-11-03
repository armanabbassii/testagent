/*
 * src/test_execution/newman/runner.js — run one Newman collection for the Python engine.
 *
 * Usage: node runner.js <config.json>
 *
 * The config (written by engine.py into a private temporary directory) names the
 * collection, environment and globals files, the Newman module and the paths of the
 * raw JSON / HTML reports and of an NDJSON event log — all inside that private
 * directory. Raw files are never written anywhere else; the Python side sanitizes
 * them before anything reaches the artifact directory, and deletes them afterwards.
 *
 * The event log is appended synchronously, one line per event, so the requests that
 * already ran are on disk even if the process is killed by the overall timeout.
 * Nothing is printed except fatal wrapper errors (no CLI reporter).
 *
 * Runtime environment variables (e.g. the access token) arrive in the process
 * environment as TESTAGENT_NEWMAN_ENV_VARS — a JSON array of {key, value} — never in
 * the config file. They are removed from process.env at once and handed to Newman as
 * `envVar`, which Newman applies after loading the environment file, so they take
 * precedence over that file's values without the file ever being modified.
 *
 * Exit codes: 0 completed without failures, 1 completed with failures,
 *             2 Newman reported an error, 3 the run reported run.error,
 *             4 the wrapper could not start Newman (bad config, missing module),
 *             5 the parent (Python engine) process disappeared.
 */
'use strict';

const fs = require('fs');

function fail(message) {
    process.stderr.write(`runner error: ${message}\n`);
    process.exit(4);
}

if (process.argv.length < 3) {
    fail('missing config path');
}

let config;
try {
    config = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
} catch (err) {
    fail(`cannot read config (${err.name})`);
}

// Read and drop the runtime variables before anything else can see or inherit them.
let envVar = [];
const runtimeVars = process.env.TESTAGENT_NEWMAN_ENV_VARS;
delete process.env.TESTAGENT_NEWMAN_ENV_VARS;
if (runtimeVars !== undefined) {
    try {
        envVar = JSON.parse(runtimeVars);
    } catch (err) {
        fail(`cannot read runtime variables (${err.name})`);
    }
    if (!Array.isArray(envVar) || !envVar.every(function (v) {
        return v && typeof v.key === 'string' && typeof v.value === 'string';
    })) {
        fail('invalid runtime variables');
    }
}

let newman;
try {
    newman = require(config.newman_module);
} catch (err) {
    fail(`cannot load newman (${err.code || err.name})`);
}

function record(entry) {
    fs.appendFileSync(config.events_path, JSON.stringify(entry) + '\n', 'utf8');
}

function errorInfo(err) {
    if (!err) {
        return null;
    }
    return { name: err.name || '', code: err.code || '', message: String(err.message || err) };
}

const itemIndex = new Map();

function itemOf(o) {
    const item = o && o.item;
    const id = item && item.id ? item.id : '';
    if (id && !itemIndex.has(id)) {
        itemIndex.set(id, itemIndex.size);
    }
    return { item_id: id, item_name: item && item.name ? item.name : '', index: itemIndex.get(id) };
}

record({ type: 'start', pid: process.pid });

// If the Python engine dies (killed, crashed), stop instead of running on as an orphan.
// The interval is unref'd so it never keeps a finished run alive.
if (Number.isInteger(config.parent_pid) && config.parent_pid > 0) {
    setInterval(function () {
        try {
            process.kill(config.parent_pid, 0);
        } catch (err) {
            if (err.code !== 'EPERM') {
                process.exit(5);
            }
        }
    }, 1000).unref();
}

const emitter = newman.run(
    {
        collection: config.collection,
        environment: config.environment,
        globals: config.globals,
        envVar: envVar,
        timeoutRequest: config.timeout_request_ms,
        delayRequest: config.delay_request_ms,
        bail: config.bail,
        insecure: config.insecure,
        color: 'off',
        disableUnicode: true,
        reporters: ['json', 'htmlextra'],
        reporter: {
            json: { export: config.json_report },
            htmlextra: Object.assign({ export: config.html_report }, config.htmlextra || {}),
        },
    },
    function (err, summary) {
        const run = summary && summary.run ? summary.run : {};
        const failures = Array.isArray(run.failures) ? run.failures.length : 0;
        record({
            type: 'done',
            error: errorInfo(err),
            run_error: errorInfo(run.error),
            failures: failures,
        });
        if (err) {
            process.exitCode = 2;
        } else if (run.error) {
            process.exitCode = 3;
        } else {
            process.exitCode = failures ? 1 : 0;
        }
    }
);

emitter.on('beforeItem', function (err, o) {
    record(Object.assign({ type: 'item_start' }, itemOf(o)));
});

emitter.on('request', function (err, o) {
    const response = o && o.response;
    const request = o && o.request;
    record(Object.assign({ type: 'request' }, itemOf(o), {
        method: request && request.method ? request.method : '',
        url: request && request.url ? request.url.toString() : '',
        status_code: response ? response.code : null,
        status_text: response ? response.status : null,
        response_time_ms: response ? response.responseTime : null,
        error: errorInfo(err),
    }));
});

emitter.on('assertion', function (err, o) {
    record(Object.assign({ type: 'assertion' }, itemOf(o), {
        assertion: o && o.assertion ? String(o.assertion) : '',
        skipped: Boolean(o && o.skipped),
        error: errorInfo(err),
    }));
});

emitter.on('script', function (err, o) {
    if (!err) {
        return;
    }
    record(Object.assign({ type: 'script_error' }, itemOf(o), {
        event: o && o.event && o.event.listen ? o.event.listen : '',
        error: errorInfo(err),
    }));
});

emitter.on('item', function (err, o) {
    record(Object.assign({ type: 'item_done' }, itemOf(o)));
});
