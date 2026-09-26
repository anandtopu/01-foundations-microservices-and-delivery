// M5 gate: a 50-VU burst of rate quotes against the gateway (spec M5, ADR-P01-1).
//
//   k6 run load/quotes.js                       # against http://localhost:8000
//   BASE_URL=http://gw:8000 k6 run load/quotes.js
//
// What we measure is NOT our latency: it is what Meridian's service saw. Read the SOAP mock's
// GET /__stats afterwards: peak_concurrency must stay <= 4 (the bulkhead), whatever k6 sends.
// Expected mix under this burst: some 201s, many fast 503s (Retry-After) once the 4 slots are busy.
import http from "k6/http";
import { check } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
// Since M6 every call needs a shipper key (`make dev-keys` registers the .env dev key).
const API_KEY = __ENV.API_KEY || "dev-shipper-key";

export const options = {
  scenarios: {
    burst: { executor: "constant-vus", vus: 50, duration: __ENV.DURATION || "20s" },
  },
  thresholds: {
    // Every response is a contract outcome: 201, or a fast 503 with Retry-After. Nothing else.
    checks: ["rate==1.0"],
    // Shedding must be fast: a 503 that took seconds would still tie up the shipper.
    "http_req_duration{status:503}": ["p(95)<500"],
    // Both outcomes must actually happen, or the burst proved nothing (a run with zero 503s would
    // pass the latency threshold vacuously). These also print the 201/503 split in the summary.
    "http_reqs{status:201}": ["count>0"],
    "http_reqs{status:503}": ["count>0"],
  },
};

export default function () {
  const key = `k6-${__VU}-${__ITER}-${Date.now()}`; // unique: M6 would replay a reused key
  const res = http.post(
    `${BASE_URL}/v1/rate-quotes`,
    JSON.stringify({
      origin_zip: "30301",
      dest_zip: "60601",
      weight_lb: 1200,
      service_level: "LTL_STANDARD",
    }),
    {
      headers: { "Content-Type": "application/json", "Idempotency-Key": key, "X-API-Key": API_KEY },
      tags: { name: "POST /v1/rate-quotes" },
    },
  );
  check(res, {
    "201 or 503": (r) => r.status === 201 || r.status === 503,
    // RFC 9110 delay-seconds: a positive whole number, never 0 or a date.
    "503 carries Retry-After >= 1": (r) =>
      r.status !== 503 || /^[1-9][0-9]*$/.test(r.headers["Retry-After"] || ""),
  });
}
