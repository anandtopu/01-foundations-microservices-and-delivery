// M9 (spec section 7, load row): shipment reads at a constant ARRIVAL rate.
//
//   k6 run load/reads.js                        # 200 req/s for 5 min against http://localhost:8000
//   RATE=50 DURATION=2m k6 run load/reads.js    # scaled down if the VM cannot sustain 200/s
//
// Pass threshold (spec): read p95 < 150 ms. constant-arrival-rate (not constant VUs) keeps offering
// 200 req/s even when responses slow down; a closed loop would quietly lower the rate instead and
// hide the latency (coordinated omission). Half the requests fetch one shipment, half list a page.
import http from "k6/http";
import { check } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const API_KEY = __ENV.API_KEY || "dev-shipper-key";
const RATE = parseInt(__ENV.RATE || "200", 10);

export const options = {
  scenarios: {
    reads: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: __ENV.DURATION || "5m",
      preAllocatedVUs: 50,
      maxVUs: 300,
    },
  },
  thresholds: {
    http_req_duration: ["p(95)<150"], // the spec's read SLO
    http_req_failed: ["rate<0.001"], // any non-2xx read counts as an error
    dropped_iterations: ["count==0"], // k6 could not keep up: the offered rate was not delivered
  },
};

export function setup() {
  // IDs to fetch: the first page of ACME's shipments.
  const res = http.get(`${BASE_URL}/v1/shipments?limit=200`, { headers: { "X-API-Key": API_KEY } });
  const ids = res.json("data").map((s) => s.shipment_id);
  if (ids.length === 0) throw new Error("no shipments to read: ingest a file first");
  return { ids };
}

export default function (data) {
  const headers = { "X-API-Key": API_KEY };
  const res =
    __ITER % 2 === 0
      ? http.get(`${BASE_URL}/v1/shipments/${data.ids[__ITER % data.ids.length]}`, {
          headers,
          tags: { name: "GET /v1/shipments/{id}" },
        })
      : http.get(`${BASE_URL}/v1/shipments?limit=50`, {
          headers,
          tags: { name: "GET /v1/shipments" },
        });
  check(res, { "200": (r) => r.status === 200 });
}
