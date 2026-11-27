import http from 'k6/http';
import exec from 'k6/execution';
import { check, sleep } from 'k6';
import { Counter, Rate, Trend } from 'k6/metrics';

const latency = { fresh: new Trend('fresh_ms', true), replay: new Trend('replay_ms', true) };
const errors = { fresh: new Rate('fresh_errors'), replay: new Rate('replay_errors') };
const accepted = { fresh: new Counter('fresh_202'), replay: new Counter('replay_202') };
const hold = Number(__ENV.HOLD_SECONDS || 35);
const profile = (target, execName) => ({
  executor: 'ramping-arrival-rate', exec: execName, startRate: 1, timeUnit: '1s',
  preAllocatedVUs: 16, maxVUs: 64,
  stages: [{target, duration: '10s'}, {target, duration: `${hold}s`},
           {target: 0, duration: '10s'}],
});
export const options = {
  scenarios: {
    fresh: {
      executor: 'ramping-vus', exec: 'fresh', startVUs: 0,
      stages: [{target: Number(__ENV.FRESH_VUS || 3), duration: '10s'},
               {target: Number(__ENV.FRESH_VUS || 3), duration: `${hold}s`},
               {target: 0, duration: '10s'}],
    },
    replay: profile(Number(__ENV.REPLAY_RATE || 60), 'replay'),
  },
  thresholds: {
    http_req_failed: ['rate<0.01'], dropped_iterations: ['count==0'],
    fresh_ms: ['p(95)<1000'], replay_ms: ['p(95)<1000'],
    fresh_errors: ['rate<0.01'], replay_errors: ['rate<0.01'],
  },
  summaryTrendStats: ['med', 'p(95)', 'p(99)'],
};

function event(id, observedAt) {
  return { event_id: id, namespace: 'target-apps', pod_name: __ENV.POD_NAME,
    pod_uid: __ENV.POD_UID, incident_type: 'Http5xxSpike', observed_at: observedAt,
    source: 'http_metrics', summary: 'Synthetic 5xx counter signal for local load testing' };
}

function request(id, key, observedAt) {
  return {
    method: 'POST',
    url: `${__ENV.API_URL}/incidents`,
    body: JSON.stringify(event(id, observedAt)),
    params: { headers: {Authorization: `Bearer ${__ENV.INGEST_TOKEN}`,
              'Content-Type': 'application/json', 'Idempotency-Key': key}, timeout: '5s' },
  };
}

function send(id, key, observedAt) {
  const req = request(id, key, observedAt);
  return http.request(req.method, req.url, req.body, req.params);
}

export function setup() {
  const observedAt = new Date().toISOString();
  const id = `${__ENV.RUN_ID}:replay`;
  const response = send(id, id, observedAt);
  if (response.status !== 202) throw new Error(`Replay setup returned ${response.status}`);
  return { observedAt, incident: response.json('incident_id') };
}

function record(name, response, original) {
  const ok = check(response, {
    accepted: r => r.status === 202 && r.headers.Location === `/incidents/${r.json('incident_id')}`,
    same_incident: r => !original || (r.status === 202 && r.json('incident_id') === original),
  });
  latency[name].add(response.timings.duration);
  errors[name].add(!ok);
  accepted[name].add(ok ? 1 : 0);
}

export function fresh(data) {
  const batch = [0, 1].map(offset => {
    const id = `${__ENV.RUN_ID}:fresh:${exec.scenario.iterationInTest * 2 + offset}`;
    return request(id, id, data.observedAt);
  });
  for (const response of http.batch(batch)) record('fresh', response);
  sleep(3);
}

export function replay(data) {
  const id = `${__ENV.RUN_ID}:replay`;
  record('replay', send(id, id, data.observedAt), data.incident);
}

export function handleSummary(data) {
  return {stdout: JSON.stringify({
    duration_ms: data.state.testRunDurationMs,
    metrics: Object.fromEntries(Object.entries(data.metrics).map(([key, entry]) =>
      [key, {values: entry.values, thresholds: entry.thresholds}]))
  }) + '\n'};
}
