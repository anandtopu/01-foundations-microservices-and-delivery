# Ground-Truth Digest - verified versions, dates and stale-knowledge traps (as of 2026-09-24)

> Compiled from the nine research notes in this folder (RN01-RN09). Each entry is traceable to a source in the corresponding RN file. Read this before you trust anything from memory or from an older curriculum.

HOW TO USE THIS DIGEST
- These facts were verified against live sources on 2026-09-24 by the research phase. They override your training data.
- Reconciled conflicts between notes:
  * Kubernetes: latest minor is 1.37 (1.37.0 released 2026-08-26, 1.37.1 on 2026-09-23). Upstream patch support covers 1.35-1.37; 1.34 reaches EOL 2026-10-27. Managed services lag: EKS standard support is 1.34-1.36 (1.37 not yet on EKS); AKS 1.37 is in preview (GA Oct 2026).
  * K3s/RKE2 track Kubernetes minors; v1.37.0+k3s1 and v1.37.0+rke2r1 exist (2026-09-14). Treat "K3s 1.36.x" in RN08 as the previous line.
  * DRA (resource.k8s.io/v1) is GA/stable; the notes differ on whether the GA minor was 1.34 or 1.35, so do not cite a specific GA minor. Say "GA and stable as of 1.37".
  * Gateway API: v1.6 is current (v1.6.0 GA 2026-06-30, v1.6.2 latest). TCPRoute and UDPRoute are Standard.
- Fast-churning facts (model names, model prices, framework patch versions): teach durable concepts first. When you name a specific current model or version, say "as of September 2026" and do not build exercises that break if it changes. Prefer "a current frontier model (e.g. ...)" over hard-coding.
- Naming: the US Department of Defense has also used the style "Department of War" in official releases since 2025. Write "DoD (styled 'Department of War' in some 2025-2026 releases)" once, then "DoD".
- Do not state any of the "stale-knowledge traps" below as current fact.


## RN01-fde-role-and-market
VERSIONS/STATUS:
- a16z 'Trading Margin for Moat' (Joe Schmidt): published (as of 2025-06-04)
- Pragmatic Engineer FDE explainer: published (as of 2025-08-12)
- SVPG / Marty Cagan 'Forward Deployed Engineers': published (as of 2025-09-17)
- Salesforce FDE program (committed to 1,000 FDEs): launched April 2025; commitment stated in Salesforce blog (as of 2026-03-27)
- Anthropic enterprise AI services company (Blackstone, H&F, Goldman), later named Ode with Anthropic: announced; named 2026-07-15 (CEO Chris Taylor, ~100 engineers) (as of 2026-05-04)
- OpenAI Deployment Company plus Tomoro acquisition (~150 FDEs): launched; valuation contested ($10B per TechCrunch vs $14B per Pragmatic Engineer) (as of 2026-05-11)
- Google Cloud FDE unit ('hundreds' of hires): reported (as of 2026-05-12)
- AWS Forward Deployed Engineering unit ($1B): launched (as of 2026-06-30)
- Microsoft Frontier Co. ($2.5B, 6,000+ people): launched; says it goes beyond the FDE label (as of 2026-07-02)
- Business Insider / Indeed FDE posting index: 5,230% above Jan-2025 baseline by April 2026; index values, not counts (corrected) (as of 2026-05-18)
- Bloomberry analysis of 1,000 FDE postings: 1,165% YoY; median base $173,816 (as of 2025-11-18 (updated 2026-01-25))
- Palantir Lever board (Delta/Echo/Dev labels; 56 FDSE postings): live snapshot (as of 2026-09-24)
- OpenAI Forward Deployed Engineering department (27 postings, 11 cities): live snapshot (as of 2026-09-24)
- Anthropic Forward Deployed Engineer posting ($280K-$320K): live (as of 2026-09-24)
- Levels.fyi FDE median total comp $206,073: live (as of 2026-09-24)
STALE-KNOWLEDGE TRAPS:
- Treating FDE as a Palantir-only quirk. By 2026 it exists at OpenAI, Anthropic, AWS, Google Cloud, Microsoft (under a different label), Salesforce, Databricks, Snowflake, Cohere, ElevenLabs, Scale, Glean, Ramp, Sierra, Decagon, Harvey, Anduril and Shield AI.
- Saying AI labs only sell APIs and leave implementation to partners. OpenAI (Deployment Company plus the Tomoro acquisition) and Anthropic (Ode with Anthropic) launched services joint ventures in May 2026.
- Saying hyperscalers rely only on Solutions Architects, professional services and partners. AWS put $1B into an FDE unit (2026-06-30), Microsoft launched Frontier Co. (2026-07-02) and Google Cloud formed an FDE unit (May 2026).
- Saying Anthropic has no FDE title. Explicit Forward Deployed Engineer postings now exist inside its Applied AI team, alongside pre-sales Applied AI Architects.
- Describing OpenAI's FDE team as a small San Francisco group. It now spans 11 cities, with vertical FDEs, Deployment Leads, managers, and Gov and Security variants.
- Framing FDE deliverables as RAG chatbots. 2026 postings name MCP servers, sub-agents, agent skills, multi-agent orchestration, eval frameworks, red-teaming, security models and runbooks.
- Quoting '643 to 5,330 postings'. Business Insider's Indeed figures are index values against a Jan-2025 baseline, and BI issued a correction.
- Assuming FDE pays less than SWE. Anthropic's FDE base is $280K-$320K and OpenAI's Forward Deployed Security Engineer tops out at $445K.
- Quoting Palantir travel as '0-100%'. Current commercial postings say up to 25% and flexible; US Government and intern tracks say 25-50%.
- Finding FDE jobs by searching the title alone misses equivalents: Agent Engineer/Strategist, Agent Deployment Engineer, Legal Engineer, Mission Software Engineer, Technical Operations Engineer, Applied AI Engineer, Delivery Engineering, Deployment Strategist/Lead, Forward Deployed PM.
- Saying Microsoft calls its people FDEs. Microsoft says Frontier Co. goes 'beyond' the FDE label.
- Reading 'Salesforce stopped hiring engineers' as no engineering hiring. Core engineering is flat at ~15,000, but Salesforce committed to building a team of 1,000 FDEs.
- Relying on older interview-loop prep. Google Cloud reportedly cut its FDE loop to 2 interviews in 2 days in 2026.
- Citing job postings without an as-of date. Postings churn quickly; one Snowflake FDE posting returned HTTP 410 during this research.

## RN02-fde-interview-processes
VERSIONS/STATUS:
- Anthropic Candidate AI Guidance: Current; no AI in take-homes or live interviews unless stated; refine (not generate) application drafts with Claude (as of 2025-07-10 (last updated))
- Anthropic 'Designing AI-resistant technical evaluations' (performance take-home): Published; AI tools allowed on that take-home; test redesigned twice; original open-sourced (as of 2026-01-21)
- Anthropic Forward Deployed Engineer (Applied AI) posting: Open; $280K-$320K; 4+ years; about 25% travel; MCP servers/sub-agents/agent skills (as of 2026-09-24)
- Palantir FDSE (experienced) posting: Open; $135K-$200K base; 1+ years post-college; up to 25% travel (as of 2026-09-24)
- Palantir FDSE New Grad (Commercial): Open; $135K-$145K; graduating Dec 2026/Spring 2027; 25-50% travel (as of 2026-09-24)
- Scale AI Frontier Agents Engineer (Forward Deployed Engineering): Open; $180K-$225K; in office 3x/week; 90-day re-apply wait (as of 2026-09-24)
- Databricks AI Engineer – Forward Deployed Engineering (AI FDE): Open in US, US Public Sector, London, Seoul, India; US base $152,900-$210,155 (as of 2026-09-21 (listing updated))
- Databricks official interview process: Virtual on Google Meet; 7 phases; 2-3 months; onsite of 4-6 interviews; presentations for some roles (as of 2026-09-24)
- Sierra interview format: In-person interviews at offices (official) (as of 2026-09-24)
- Canva AI-assisted coding interviews: Adopted; AI tools expected; replaced CS-fundamentals screen (as of 2025-06-11)
- Meta AI-enabled coding interview: Rolled out from Oct 2025; replaces one of two onsite coding rounds (secondary sources) (as of 2025-10)
- Google Gemini code-comprehension interview pilot: Pilot for junior/mid-level roles on select US teams (reported via internal doc) (as of 2026-05)
- OpenAI beta agentic coding round: Beta pilot; not every candidate gets it (secondary source) (as of 2026)
- OpenAI Interview Guide (AI tools vary by interview; 4-6 h of finals): Current per search snippet; page returned 403 on fetch (as of 2026-09 (snippet))
STALE-KNOWLEDGE TRAPS:
- 'AI in interviews is always cheating' is outdated: Meta, Canva, the Google pilot, OpenAI's beta agentic round, Distyl and Anthropic's performance take-home allow or expect AI in specific rounds. Palantir and Anthropic live rounds still forbid it, so the rule is per round.
- 'Anthropic bans AI in applications' reflects early 2025. Since 2025-07-10: draft yourself, refine with Claude, prepare with Claude, and no AI in take-homes or live rounds unless told.
- 'FDE is a Palantir-only role' is wrong in 2026: OpenAI, Anthropic, Scale, Databricks (new AI FDE org), Sierra, Decagon and others hire for it, under different titles.
- Anthropic's customer engineers are no longer just 'Solutions Architects': current titles are Forward Deployed Engineer, Applied AI Engineer and Applied AI Architect, with different pay bands.
- Scale AI's FDE is no longer data-labeling ops: current postings are 'Frontier Agents Engineer (FDE)', focused on agent runtimes, MCP and evals.
- Databricks field roles are no longer only RSA/SSA: 'AI Engineer – Forward Deployed Engineering (AI FDE)' roles now exist, including US Public Sector.
- LeetCode-Hard grinding is the wrong prep: FDE loops use practical, progressive, data-flavoured coding and debugging of existing codebases, and verbatim LeetCode is the easiest format to cheat on.
- Decomposition is not system design: it starts with no spec and scores scoping, data modeling, a thin slice and adoption.
- Take-homes are not judged on the artifact alone: loops weight the recorded walkthrough and live defense, including what you cut and how you used AI.
- Neither 'all interviews are remote' nor 'everyone is back onsite' holds: Anthropic and Databricks use Google Meet, while Sierra interviews in person.
- Eval literacy and MCP are now explicit requirements or deliverables in FDE postings (OpenAI eval-driven success metrics, Anthropic FDE, Scale), and pre-2025 curricula omit them.
- Model lists in AI-enabled rounds (e.g., Meta's GPT-5, Claude 4.x, Gemini 2.5 Pro, Llama 4) change quickly, so don't hard-code tool names.

## RN03-regulated-industries-and-deployment-models
VERSIONS/STATUS:
- CMMC 48 CFR acquisition rule: Effective Nov 10, 2025 (Phase 1) (as of 2025-11-10)
- CMMC Phase II (C3PAO mandatory): Suspended July 13, 2026 (as of 2026-07-13)
- NIST SP 800-171: Rev 3 final May 2024; CMMC still enforces Rev 2 (as of 2026-09)
- NIST SP 800-53: Release 5.2.0 (as of 2025-08-27)
- FedRAMP 20x: Phase 3 active; new Rev5 certifications end June 11, 2027; FedRAMP Ready retired July 28, 2026 (as of 2026-09-24)
- GovRAMP (ex-StateRAMP): Renamed (dba) Feb 14, 2025 (as of 2025-02-14)
- DoD CC SRG: Released June 14, 2024 (Rev 5-aligned; IL5 requires FedRAMP High) (as of 2024-06-14)
- PCI DSS: v4.0.1 only active; future-dated requirements mandatory since Mar 31, 2025 (as of 2025-03-31)
- EU DORA: Applies since Jan 17, 2025; 19 CTPPs designated Nov 18, 2025 (as of 2025-11-18)
- NYDFS 23 NYCRR 500: Final phase effective Nov 1, 2025 (as of 2025-11-01)
- SR 26-2 / OCC Bulletin 2026-13: Supersedes SR 11-7; GenAI/agentic AI out of scope (as of 2026-04-17)
- CFPB Section 1033: Enjoined; under reconsideration; NPRM at OIRA Aug 6, 2026 (as of 2026-08-06)
- HIPAA Security Rule NPRM (RIN 0945-AA22): Proposed; final action targeted July 2027 (as of 2026-07-13)
- FDA QMSR: Effective Feb 2, 2026 (as of 2026-02-02)
- FDA AI-enabled device software functions guidance: Draft (Jan 2025) (as of 2026-09-24)
- HITRUST CSF: v11.8.0 (as of 2026-09-24)
- EU AI Act Digital Omnibus: Regulation (EU) 2026/1744 in force July 27, 2026; high-risk Dec 2, 2027 / Aug 2, 2028 (as of 2026-07-27)
- Digital Omnibus (GDPR/data part): Pending; no trilogues (as of 2026-09-08)
- EU Data Act: Applies since Sept 12, 2025; switching/egress fees banned from Jan 12, 2027 (as of 2026-09-24)
- EU-US Data Privacy Framework: Valid; upheld by General Court Sept 3, 2025; CJEU appeal C-703/25 P pending (as of 2025-12-01)
- AWS European Sovereign Cloud: GA Jan 15, 2026 (Brandenburg) (as of 2026-01-15)
- AWS Snowball: Commercial-Region support discontinued Dec 31, 2026 (as of 2026-09-24)
- Zarf: v0.86.0 (pre-1.0, OpenSSF) (as of 2026-09-17)
- Replicated Embedded Cluster: v2.19.13 (v2 line); v3 Beta; kURL existing customers only (as of 2026-09-24)
- RKE2 / k3s: v1.37.0+rke2r1 / v1.37.0+k3s1 (as of 2026-09-14)
- Talos Linux: v1.14.1 (Kubernetes 1.37.0) (as of 2026-09-15)
- OpenShift docs (disconnected): 4.22 latest; oc-mirror v2 (as of 2026-09-24)
- Harbor: v2.15.2 stable; v2.15.3-rc2 pre-release (as of 2026-09-24)
- Hauler: v2.1.1 (as of 2026-09-24)
STALE-KNOWLEDGE TRAPS:
- Treating SR 11-7 as current US model-risk guidance. It was superseded by SR 26-2 on Apr 17, 2026, and generative/agentic AI is excluded from the new scope.
- Saying EU AI Act high-risk obligations apply Aug 2, 2026. They now apply Dec 2, 2027 (Annex III) and Aug 2, 2028 (Annex I) under Regulation (EU) 2026/1744.
- Assuming the Digital Omnibus already changed GDPR. Only the AI half is law; the GDPR/data half is still being negotiated.
- Saying CMMC C3PAO Level 2 certification is mandatory from Nov 10, 2026. Phase II was suspended July 13, 2026.
- Saying CMMC is based on NIST 800-171 Rev 3. It is still Rev 2; the Rev 3 rulemaking has no date.
- Describing FedRAMP as Rev5 agency ATO or JAB plus 'FedRAMP Ready'. It is now 20x Classes A-D with Agency or Program paths and CR26; FedRAMP Ready was retired July 28, 2026, and Rev5 new certifications end June 11, 2027.
- Calling it StateRAMP. It has operated as GovRAMP since Feb 14, 2025.
- Teaching PCI DSS v4.0, or treating future-dated requirements as optional. v4.0.1 is the only version, all requirements have been mandatory since Mar 31, 2025, and SAQ A changed.
- Saying the HIPAA Security Rule update is final. It is still an NPRM, targeted for July 2027.
- Saying CFPB 1033 compliance begins Apr 2026. Enforcement is enjoined and the rule is under reconsideration.
- Describing DORA as upcoming. It has applied since Jan 17, 2025, and 19 critical ICT third-party providers were designated Nov 18, 2025.
- Saying IL5 can be reached via FedRAMP Moderate plus DoD controls. The 2024 CC SRG requires FedRAMP High.
- Saying no frontier LLMs are available in classified clouds. Azure OpenAI is available at Secret and Top Secret, and Gemini runs on GDC air-gapped.
- Using the name 'Vertex AI'. Google now brands it Gemini Enterprise Agent Platform, including in its BAA.
- Calling the AWS European Sovereign Cloud 'planned'. It has been GA since Jan 15, 2026.
- Recommending AWS Snowball for edge/DDIL. Commercial-Region support ends Dec 31, 2026.
- Recommending Replicated kURL for new vendors. Embedded Cluster (k0s) or Helm plus the SDK is the path.
- Teaching OpenShift oc-mirror v1. v2 is current, and the docs are at 4.22.
- Referring to NIST 800-53 as 5.1.x. The current catalog is Release 5.2.0 (Aug 27, 2025).
- Referring to the FDA QS regulation and QSIT. The QMSR has been effective since Feb 2, 2026.
- Saying the FDA AI lifecycle guidance is final. It is still draft; the PCCP guidance is final.
- Treating the EU Data Act as future law. It has applied since Sept 12, 2025, and switching fees are banned from Jan 12, 2027.
- Using the DoD / public.cyber.mil naming. Official releases say 'Department of War' (war.gov), and the STIG library moved to www.cyber.mil.
- Using old Zarf CLI or flags. Zarf is at v0.86.0 with frequent breaking changes; yolo mode is deprecated.

## RN04-ai-deployment-stack
VERSIONS/STATUS:
- Claude Opus 5.5 (claude-opus-5-5): Active; recommended default; $4/$20 per MTok; 1M context; retirement not sooner than 2027-09-22 (as of 2026-09-24)
- Claude Fable 5.1 / Sonnet 5 / Haiku 4.5: Active; $10/$50, $2/$10 (introductory price made standard), $1/$5 (as of 2026-09-24)
- OpenAI GPT-6 Astra / Sol / Luna: Current flagships; Astra 2026-09-03, Sol and Luna 2026-09-22; 1.05M context (as of 2026-09-24)
- OpenAI Assistants API: Shut down 2026-08-26 (replaced by Responses + Conversations APIs) (as of 2026-09-24)
- Gemini 3.8 Flash: GA 2026-09-02 (as of 2026-09-24)
- Grok 4.7: Released Sept 2026 on xAI API; 500k context; $2/$6 (as of 2026-09-24)
- DeepSeek V4-Pro: Open weights, MIT; 1.6T/49B active; 1M context; late April 2026 (as of 2026-09-24)
- Gemma 4: Released 2026-04-02 under Apache 2.0 (press date) (as of 2026-09-24)
- vLLM: v0.30.0 released 2026-09-22; V0 removed in v0.11.0 (2025-10-02); Model Runner V2 default since v0.29.0 (as of 2026-09-24)
- SGLang: v0.5.20 released 2026-09-18 (as of 2026-09-24)
- TensorRT-LLM: Stable v1.2.1 (2026-04-20); v1.3.0rc28 prerelease (2026-09-23) (as of 2026-09-24)
- NVIDIA Dynamo: v1.5.0 released 2026-09-21; Apache 2.0 (as of 2026-09-24)
- Hugging Face TGI: Maintenance mode; last release v3.3.7 (2025-12-19) (as of 2026-09-24)
- llm-d: v0.9.0 released 2026-08-17; CNCF Sandbox (as of 2026-09-24)
- Gateway API Inference Extension: v1.6.2 released 2026-09-17; InferencePool v1 stable; v1.0.0 was 2025-09-09 (as of 2026-09-24)
- Kubernetes: v1.37.1 released 2026-09-23 (v1.37.0 on 2026-08-26); docs say DRA stable since v1.35 (as of 2026-09-24)
- NVIDIA GPU Operator: 26.7.1 released 2026-09-23; B300/GB300 support; DRA driver via GPUCluster CR (as of 2026-09-24)
- NVIDIA Vera Rubin NVL72: In full production per NVIDIA; 72 Rubin GPUs, 20.7 TB HBM4 (as of 2026-09-24)
- LiteLLM: v1.102.1 released 2026-09-23; 1.82.7/1.82.8 absent from PyPI (as of 2026-09-24)
- Agent Router (formerly Envoy AI Gateway): Renamed; Agentic AI Foundation project; v1.1.0 released 2026-08-21 (as of 2026-09-24)
- MCP specification: Latest revision 2026-07-28 (stateless); previous 2025-11-25 (as of 2026-09-24)
- MCP governance: Donated to the Agentic AI Foundation (Linux Foundation) on 2025-12-09 (as of 2026-09-24)
- A2A protocol: v1.0.0 released 2026-03-12; joined the Agentic AI Foundation 2026-08-27 (as of 2026-09-24)
- Agent frameworks (PyPI): langgraph 1.2.12; google-adk 2.9.2; pydantic-ai 2.49.0; agent-framework 1.19.0; crewai 1.15.22; openai-agents 0.22.3; claude-agent-sdk 0.2.159 (as of 2026-09-24)
- Microsoft Foundry: Renamed from Azure AI Foundry; Agents v2 on Responses API (as of 2026-09-24)
- Amazon Bedrock AgentCore: 13 services including Harness, Policy, Registry, Payments, Evaluations, Optimization (as of 2026-09-24)
- pgvector: 0.8.6 released 2026-07-29 (as of 2026-09-24)
- Milvus: 3.0 released 2026-07-29; 3.0.2 released 2026-09-20 (as of 2026-09-24)
- OpenTelemetry GenAI semantic conventions: Development status; moved to semantic-conventions-genai repo (created 2026-05-05) (as of 2026-09-24)
- OWASP Top 10 for Agentic Applications 2026: Published 2025-12-09 (ASI01-ASI10) (as of 2026-09-24)
STALE-KNOWLEDGE TRAPS:
- Treating GPT-4o, Claude 3.5 Sonnet or Gemini 1.5/2.0 as current. They are retired or retiring (Gemini 2.0 Flash shut down 2026-06-01; GPT-4o snapshots retire Oct 2026).
- Using temperature=0 for determinism. It returns 400 on Claude 4.7+ and is deprecated on Gemini 3.6 Flash+; teach effort plus structured outputs instead.
- Extended thinking with budget_tokens. It is deprecated on Claude 4.6 and not accepted on later models; use adaptive thinking plus effort.
- OpenAI Assistants API. It shut down 2026-08-26; use the Responses and Conversations APIs. Azure also moved agents to the Responses API (Agents v2).
- Azure AI Foundry / Azure AI Studio naming. It is now Microsoft Foundry and Foundry Tools. Vertex AI Agent Engine is now documented as Gemini Enterprise Agent Platform.
- vLLM V0 vs V1 and VLLM_USE_V1. V0 was removed in v0.11.0 (Oct 2025), Model Runner V2 is default since v0.29.0, and `python -m vllm.entrypoints.openai.api_server` is deprecated in favor of `vllm serve`.
- Recommending Hugging Face TGI for new deployments. It is in maintenance mode.
- GPU scheduling taught only as device plugin plus nvidia.com/gpu. DRA (resource.k8s.io/v1) is stable, and the GPU Operator manages the DRA driver.
- Gateway API Inference Extension taught with InferenceModel/InferenceObjective CRDs. InferencePool v1 is the stable API; the alpha APIs and the full endpoint picker moved to llm-d in v1.6.0.
- Envoy AI Gateway and Portkey described as they were. Envoy AI Gateway is now Agent Router (an Agentic AI Foundation project), and Portkey says it is part of Palo Alto Networks Prisma AIRS.
- MCP taught as stateful sessions with initialize, SSE resumability, sampling, roots and dynamic client registration. The 2026-07-28 spec is stateless and deprecates all of these. MCP is also governed by the Agentic AI Foundation, not Anthropic, since Dec 2025.
- A2A taught as a Google v0.x draft. v1.0 shipped 2026-03-12 and it is now an Agentic AI Foundation project.
- Semantic Kernel/AutoGen as the Microsoft agent stack. Microsoft Agent Framework is the successor. Claude Code SDK is now named Claude Agent SDK.
- Llama as the default open model, or Gemma/Kimi/GLM licenses described as they were. There is no Llama 5, Gemma 4 is Apache 2.0, and Kimi K3 and GLM-5.3 have bespoke commercial thresholds.
- OTel GenAI conventions located in the main semconv repo, or described as stable. They moved to a separate repo in 2026 and remain in Development status.
- OWASP LLM Top 10 2023 as the reference. Use the 2025 list plus the OWASP Top 10 for Agentic Applications 2026.
- H100 as the top GPU. Blackwell and Blackwell Ultra are mainstream in clouds, and Vera Rubin NVL72 is in production.
- Assuming long context has a surcharge above 200k tokens. Claude 4.6+ bills the full 1M at standard price. Per-token comparisons also miss the Claude 4.7+ tokenizer, which produces about 30% more tokens.

## RN05-cloud-kubernetes-devops-iac
VERSIONS/STATUS:
- Kubernetes: v1.37.0 latest (released 2026-08-26); supported 1.34–1.37; 1.34 EOL 2026-10-27 (as of 2026-09-24)
- Ingress-NGINX: Retired 2026-03-24 (announced 2025-11-11); no further releases or security patches (as of 2026-04-22)
- Gateway API: v1.6.0 GA 2026-06-30 (TCPRoute/UDPRoute Standard); v1.5 2026-02-27 (as of 2026-08-03)
- Helm: v4.3.0 latest (4.0.0 GA 2025-11-12); Helm 3 v3.22.0 final minor, security fixes until 2027-02-10 (extended June 2026 per helm.sh; RN05 said 2026-11-11) (as of 2026-09-10)
- Argo CD: 3.5.3 stable; 3.6.0-rc1 (as of 2026-09)
- Flux: 2.9.5 latest (2.9 GA 2026-06-30) (as of 2026-08-31)
- Terraform: 1.16.4 latest stable; 1.17.0-beta2; BSL 1.1; IBM-owned since 2025-02-27 (as of 2026-09-23)
- CDK for Terraform: Sunset and archived (as of 2025-12-10)
- OpenTofu: 1.12.6 stable (1.12.0 on 2026-05-14); 1.13.0-rc1 (as of 2026-09)
- Pulumi: v3.264.0 (as of 2026-09-23)
- Crossplane: v2.x (v2.0 2025-08-12; v2.4.x current); CNCF graduated 2025-11-06 (as of 2026-09)
- Cluster API: v1.14.2 (supports workload clusters 1.31–1.37) (as of 2026-09-08)
- containerd: 2.4.0 released; 2.3.x LTS (as of 2026-09-16)
- Docker Engine: 29.8.1; containerd image store default for fresh installs (as of 2026-09-15)
- Docker Hardened Images: Free, Apache 2.0 (as of 2025-12-17)
- GitHub Actions Node 20: Removed; Node 24 default (as of 2026-09-23)
- GitHub Actions self-hosted runner charge: Postponed (was planned $0.002/min from 2026-03-01) (as of 2026)
- Jenkins LTS: 2.568.3; Java 21 minimum (as of 2026-09-02)
- GitLab: 19.4 (as of 2026-09-17)
- AWS Proton: End of support 2026-10-07 (closed to new customers 2025-10-07) (as of 2026-09-24)
- AWS App Runner: Closed to new customers; migrate to ECS Express Mode (as of 2026-09-24)
- AWS CodeCommit: Returned to GA (as of 2025-11-24)
- Aurora DSQL: GA (as of 2025-05-27)
- EKS Kubernetes versions: Standard 1.34–1.36; extended 1.31–1.33; 1.37 not yet on EKS (as of 2026-09-24)
- AKS 1.37: Preview Sept 2026, GA Oct 2026 (as of 2026-09-18)
- Azure Basic Load Balancer: Retired (as of 2025-09-30)
- CKA / CKS: Test Kubernetes v1.35; $445 each (as of 2026-09-24)
- Terraform Associate: Exam 004 (Terraform 1.12), $70.50 (as of 2026-09-24)
- Microsoft AZ-204 / AZ-500: Retired 2026-07-31 / 2026-08-31 (as of 2026-09-24)
- AWS Advanced Networking Specialty: Retiring 2026-12-31 (as of 2026-09-24)
STALE-KNOWLEDGE TRAPS:
- Recommending ingress-nginx: it was retired 2026-03-24 with no CVE fixes. F5's NGINX Ingress Controller is a different codebase and is not retired.
- Calling Gateway API TCPRoute, UDPRoute or TLSRoute experimental: all are Standard as of v1.5/v1.6. Experimental kinds moved to the gateway.networking.x-k8s.io group.
- Teaching sidecars as plain extra containers, pod resizes as requiring restarts, or GPUs as schedulable only via device plugins. Native sidecars (1.33), in-place resize (1.35) and DRA (1.34/1.37) are GA.
- Presenting IPVS as the preferred kube-proxy mode, or treating cgroup v1 or containerd 1.x as acceptable.
- Treating Helm 3 as current. Helm 4 has been GA since 2025-11-12, and Helm 3 security support ends 2027-02-10 (extended from 2026-11-11).
- Assuming Argo CD label-based tracking and RBAC inheritance to sub-resources. Both defaults changed in 3.0.
- Claiming self-hosted GitHub runners are billed $0.002/min. That charge was postponed.
- Assuming OIDC sub claims look like repo:org/name for every repo. Repos created after 2026-07-15 use an ID-based format. Also current: Node 20 actions are dead as of 2026-09-23, and pull_request_target is disabled by default from 2026-11-02.
- Calling Terraform open source or HashiCorp independent. Terraform is BSL 1.1 and IBM completed the acquisition 2025-02-27. CDKTF was archived 2025-12-10.
- Teaching Terraform Associate 003. The current exam is 004 and tests Terraform 1.12.
- Teaching Crossplane claims and patch-and-transform. Both were removed in Crossplane v2.
- Recommending tfsec. It is now part of Trivy.
- Using the names Azure AD or Azure Stack HCI, or assuming Azure VMs get default internet egress. New VNets have private subnets for API versions after 2026-03-31. Basic Load Balancer was retired 2025-09-30.
- Recommending AWS App Runner, CodeCatalyst, Cloud9 or Proton for new projects. The first three are closed to new customers, and Proton support ends 2026-10-07.
- Saying CodeCommit is deprecated. It returned to GA 2025-11-24.
- Recommending AZ-204, AZ-500, AI-102 or AI-900. All were retired in mid-2026.
- Assuming hardened base images are paid-only (DHI has been free since 2025-12-17) or that Bitnami's free Debian images are still at bitnami/* (they moved to bitnamilegacy).
- Assuming the Docker Engine overlay2 graph driver is the default and old API clients work. Engine 29 uses the containerd store and requires API 1.44 or later. Jenkins needs Java 21 or later.
- Using cloud.google.com/.../docs links for GKE. The docs redirect to docs.cloud.google.com.

## RN06-observability-and-security
VERSIONS/STATUS:
- OpenTelemetry CNCF status: Graduated 2026-05-11 (as of 2026-09-24)
- OTel Profiles signal: Public alpha (OTLP proto v1.10.0, Collector v0.148.0+) (as of 2026-03)
- OTel declarative configuration: Stable, opentelemetry-configuration 1.0.0 (as of 2026-03/04)
- OTel GenAI semconv: Development/experimental; moved to semantic-conventions-genai in semconv v1.42.0 (2026-06-12) (as of 2026-09)
- OpenTelemetry eBPF Instrumentation (OBI): v0.13.0, pre-1.0 (as of 2026-09-04)
- OTel Collector: v0.161.0 / v1.67.0 (year inferred) (as of 2026-09)
- Prometheus: 3.14.0 latest stable; 3.13 LTS; 3.15.0-rc.1; native histograms stable since 3.8.0 (as of 2026-09-21)
- Grafana: 13.2.2 current (13.0 released 2026-04-14) (as of 2026-09-15)
- Grafana Loki: 3.7.8 current; Kafka-based next-gen architecture announced, not GA as 4.0 (as of 2026-09-17)
- Grafana Mimir: 3.0 released; Kafka ingest storage stable, MQE default (as of 2025-11-03)
- Grafana Tempo: 3.0 GA (~May 2026), 3.0.3 latest, 3.1.0-rc.1 (as of 2026-09)
- Grafana Agent: End-of-life (as of 2025-11-01)
- Grafana Alloy: v1.19.2 stable; v1.20.0-rc.0 (as of 2026-09-22)
- Jaeger: v2.21.0; v1 EOL 2025-12-31 (as of 2026-09)
- Chronosphere: Acquired by Palo Alto Networks (closed) (as of 2026-01-29)
- Wiz: Acquired by Google/Alphabet (reported close) (as of 2026-03-11)
- Opsgenie: End of sale 2025-06-04; shutdown 2027-04-05 (as of 2026-09)
- OWASP Top 10: 2025 edition current (A03 Supply Chain, A10 Exceptional Conditions) (as of 2026-09)
- OWASP Top 10 for Agentic Applications: Published (2026 list) (as of 2025-12-09)
- CWE Top 25: 2025 list released (as of 2025-12-11)
- OpenSSL: 3.5 LTS to 2030-04-08; 4.0.2 current; 4.1.0-beta1; 3.0 not listed as supported (as of 2026-09-23)
- SLSA: v1.2 approved (adds Source track) (as of 2025-11-24)
- cosign: v3.1.3 (bundle format default, Rekor v2); v2.6.5 maintained (as of 2026-08-06)
- CycloneDX: 1.7; ECMA-424 published 2025-12-10 (as of 2025-10-21)
- npm token policy: Legacy tokens removed Nov 2025; granular-token direct publish removal Jan 2027 (as of 2026-09)
- GitHub OIDC immutable sub claim: Default for repos created after 2026-07-15 (as of 2026-07-15)
- Trivy ecosystem compromise: Critical advisory GHSA-69fq-xp46-6x23 (v0.69.4, trivy-action, setup-trivy) (as of 2026-03-21)
- Kyverno: 1.19.1; ClusterPolicy deprecated, removal planned 1.20 (~Nov 2026); CNCF Graduated 2026-03-16 (as of 2026-09-10)
- Kubernetes: Supported 1.35, 1.36, 1.37 (1.37.0 released 2026-08-26); MutatingAdmissionPolicy GA in 1.36 (as of 2026-09)
- ingress-nginx: Retired (no fixes after March 2026) (as of 2026-03)
- Falco: 0.45.0; legacy eBPF probe/gRPC/gVisor removed in 0.44.0 (as of 2026-09-21)
- OpenBao: v2.7.0 (ML-DSA, external keys), OpenSSF sandbox, MPL-2.0 (as of 2026-09-23)
- HashiCorp Vault: v2.1.1, BSL 1.1, IBM-owned (as of 2026-09-16)
- OAuth 2.1: Internet-Draft draft-ietf-oauth-v2-1-16 (IESG milestone Dec 2026) (as of 2026-09-03)
- WebAuthn Level 3: W3C Recommendation (as of 2026-08-25)
- EU Cyber Resilience Act: Reporting obligations live since 2026-09-11; full application 2027-12-11 (as of 2026-09-11)
STALE-KNOWLEDGE TRAPS:
- Teaching Grafana Agent: it reached EOL Nov 1, 2025. Use Alloy, which can also run standard OTel Collector YAML.
- Teaching Jaeger v1 or Jaeger clients: v1 reached EOL Dec 31, 2025. Jaeger v2 is built on the OTel Collector.
- Enabling native histograms with --enable-feature: they have been stable since Prometheus 3.8.0 and use scrape_native_histograms.
- Calling OTel incubating, saying profiles don't exist or treating declarative config as experimental: OTel graduated May 2026, profiles are in alpha and declarative config is stable at 1.0.
- Treating GenAI semconv as stable or as living in the main semconv repo: it moved to semantic-conventions-genai in June 2026 and is still experimental.
- Using OWASP Top 10 2021 categories (A10 SSRF, A06 Vulnerable Components): the 2025 list has A03 Supply Chain Failures and A10 Mishandling of Exceptional Conditions.
- Using the 2023 OWASP LLM list: use the LLM 2025 list plus the Agentic Top 10 (Dec 2025).
- Treating OpenSSL 3.0 as the supported LTS baseline: upstream support is 3.5 LTS and 4.0/3.6, and 4.0 removed ENGINE.
- Presenting PQC as future-only: hybrid X25519MLKEM768 is already the default in browsers and OpenSSL 3.5. Only PQ certificates are still pending.
- Writing Kyverno ClusterPolicy: deprecated in 1.19 with removal planned in 1.20. Use the CEL policy types or native VAP/MAP.
- Recommending ingress-nginx: it was retired after March 2026.
- Using npm classic or automation tokens in CI: they were removed Nov 2025, and granular-token direct publishing ends Jan 2027.
- Assuming the GitHub OIDC sub is repo:org/repo:...: repos created after July 15, 2026 use repo:org@ID/repo@ID:...
- Pinning GitHub Actions by tag: tj-actions and Trivy showed tags are mutable. Pin full SHAs.
- Treating security scanners and signed provenance as inherently trustworthy: the Trivy compromise (Mar 2026) and Mini Shai-Hulud (May 2026) disproved both.
- Citing SLSA v1.0, cosign v2 formats or CycloneDX 1.6 as current: the current versions are SLSA v1.2, cosign v3 bundles with Rekor v2, and CycloneDX 1.7 (ECMA-424).
- Recommending Opsgenie: end of sale June 2025, shutdown Apr 5, 2027.
- Treating Chronosphere, Wiz or HashiCorp as independent: they are owned by Palo Alto Networks, Google (reported close) and IBM, respectively.
- Calling Vault open source: it is BSL and now versioned 2.x. OpenBao is the MPL fork.
- Stating OAuth 2.1 is an RFC: it is still an Internet-Draft. RFC 9700 is the BCP.
- Listing K8s 1.32-1.34 as supported: only 1.35-1.37 are supported.
- Describing Tempo and Mimir as RF3 ingester architectures: Tempo 3 and Mimir 3 use Kafka-decoupled RF1.

## RN07-languages-databases-streaming
VERSIONS/STATUS:
- Python 3.14: Stable/bugfix; 3.14.0 released 2025-10-07; latest 3.14.7 (2026-08-05) (as of 2026-09-24)
- Python 3.15: Prerelease rc2 (2026-09-01); final scheduled 2026-10-01 (as of 2026-09-24)
- Python 3.9 / 3.10: 3.9 EOL 2025-10-31; 3.10 security-only, EOL 2026-10 (as of 2026-09-24)
- uv: 0.12.18 (2026-09-22); Astral acquisition by OpenAI announced 2026-03-19 (as of 2026-09-24)
- ty / Pyrefly / mypy: ty 0.0.84 beta; Pyrefly 1.0.0 GA 2026-05-12; mypy 2.3.1 (2026-08-15) (as of 2026-09-24)
- Go: 1.27.0 released 2026-08-19 (1.27.1 on 2026-09-01); 1.26 supported; 1.25 and older unsupported (as of 2026-09-24)
- TypeScript: 7.0 GA 2026-07-08 (Go native); 6.0 released 2026-03-23; 5.9 released 2025-08-01 (as of 2026-09-24)
- Node.js: v26 Current (LTS on 2026-10-28); v24 LTS (maintenance from 2026-10-20); v22 maintenance to 2027-04-30; v20 EOL 2026-04-30; annual majors from v27 (as of 2026-09-24)
- Bun / Deno: Bun 1.4.x (Anthropic-owned, MIT); Deno 2.9 LTS until 2027-01-31 (as of 2026-09-24)
- pnpm: 11.x (11.0 released 2026-04-28) with supply-chain defaults (as of 2026-09-24)
- PostgreSQL: 18.6 current (18.0 released 2025-09-25); 19 Beta 4 (2026-09-24); 14 EOL 2026-11-12; 13 EOL (as of 2026-09-24)
- MySQL: 9.7 LTS (2026-04-21); 8.4 LTS; 8.0 support ended 2026-04-30 (as of 2026-09-24)
- DuckDB / DuckLake: DuckDB 1.4 LTS (to 2026-11-17), 1.5 current, 2.0 planned 2026-10-21; DuckLake 1.0 (Apr 2026) (as of 2026-09-24)
- ClickHouse: 26.8 LTS (2026-08-27) (as of 2026-09-24)
- Redis / Valkey: Redis 8 tri-license including AGPLv3 (2025-05-01); Valkey 9.1 GA 2026-05-19, 9.1.2 latest (as of 2026-09-24)
- MongoDB: 8.0 major (support to 2029-10-31); rapid releases 8.x: RN07 said Atlas-only, but MongoDB official docs contradict this (review pass 2026-09-25) - check the docs before stating availability (as of 2026-09-24)
- Cassandra: 5.0.9 (2026-08-07); no 6.0 GA (as of 2026-09-24)
- MinIO community: Repository archived 2026-04-25; no longer maintained; source-only (as of 2026-09-24)
- Apache Iceberg: Spec v3 complete, v4 in development; library 1.11.0 (as of 2026-09-24)
- Apache Kafka: 4.3.1 latest (2026-06-25); 4.0 (2025-03-18) removed ZooKeeper; share groups production-ready in 4.2 (2026-02-17) (as of 2026-09-24)
- Confluent: Acquired by IBM (announced 2025-12-08, closed 2026-03-17) (as of 2026-09-24)
- Apache Flink: 2.3.0 latest (2026-06-25); 1.20 LTS (as of 2026-09-24)
- Apache Pulsar: 4.0 LTS active support ends 2026-10-21; 4.2.4 latest; 5.0.0-M2 preview (as of 2026-09-24)
- Debezium: 3.6 stable (2026-09-18); 3.7 in development (as of 2026-09-24)
- NATS server: v2.15.0 (2026-09-17); remains in CNCF under Apache 2.0 (as of 2026-09-24)
- DDIA 2nd edition: Published March 2026 (Kleppmann and Riccomini) (as of 2026-09-24)
STALE-KNOWLEDGE TRAPS:
- Free-threaded Python is officially supported since 3.14 (still optional). Describing it as 'experimental only' is outdated.
- Python 3.14 no longer uses the incremental GC; it was reverted in 3.14.5.
- Python 3.9 is EOL and 3.10 goes EOL in Oct 2026, so neither is a safe target.
- uv plus pylock.toml (PEP 751) have displaced Poetry/pip-tools as the defaults. pytest 9 uses native [tool.pytest] TOML config.
- mypy is no longer the only serious type checker: Pyrefly 1.0 is stable and ty is in beta. Astral (uv, ruff, ty) is being acquired by OpenAI.
- Go no longer needs automaxprocs in containers (1.25+). Green Tea has been the default GC since 1.26.
- Go 1.27 has generic methods, and encoding/json now runs on the v2 engine. Only Go 1.26 and 1.27 are supported.
- In TypeScript 7 the tsc command is the Go binary (tsgo was the preview name). baseUrl, moduleResolution node and target es5 are errors, and there is no compiler API until 7.1.
- Node's odd/even LTS model ends with Node 27 (annual releases, all LTS). Node 20 is EOL.
- Node runs erasable TypeScript natively (stable type stripping). --experimental-transform-types was removed in v26.
- Bun is owned by Anthropic.
- npm classic tokens were revoked in Dec 2025, and pnpm 11 enforces a 1-day minimumReleaseAge by default.
- npm install express now gives Express 5, and a bare zod import gives Zod 4.
- PostgreSQL 18 has native uuidv7() and async I/O. PG 13 is EOL and PG 19 is imminent.
- MySQL 8.0 support ended April 2026. Use 8.4 LTS or 9.7 LTS.
- Redis 8 is available under AGPLv3, so 'Redis is not open source' is outdated. Valkey is at 9.x.
- MinIO community edition is archived and unmaintained (April 2026), so it should not be the default lab S3.
- CockroachDB Core (free) was retired in 2024.
- Aurora DSQL now supports foreign keys.
- S3 supports compare-and-swap via If-Match/If-None-Match and conditional deletes.
- DynamoDB global tables offer multi-Region strong consistency (MRSC).
- Kafka no longer uses ZooKeeper (4.0+). KIP-848 rebalancing is GA, and share groups (queues) are production-ready as of 4.2.
- Confluent is owned by IBM (closed 2026-03-17).
- Flink 2.0 removed the DataSet and Scala APIs.
- NATS stayed in the CNCF under Apache 2.0.
- Iceberg spec v3 is complete, not v2.
- DDIA has a 2nd edition (2026).
- DuckDB 2.0 is due 2026-10-21, and Pulsar 4.0 LTS active support ends 2026-10-21.

## RN08-edge-networking-hybrid
VERSIONS/STATUS:
- Istio: 1.31.x current (1.31.0 announced 2026-08-31; K8s 1.32-1.36); ambient GA since 1.24 (2024-11-07) (as of 2026-09-24)
- Linkerd: 2.20 (2026-06-23); open source edge-only since Feb 2024; stable via Buoyant Enterprise for Linkerd (enterprise-2.20.3 on 2026-09-15) (as of 2026-09-15)
- Cilium: 1.20.x stable (1.20.2); 1.21.0-pre; Gateway API v1.6.1 in 1.20 (as of 2026-09 (year inferred))
- Calico: v3.32.2 latest (3.32.0 on 2026-04-30) (as of 2026-08-30)
- Ingress-NGINX: Retired; best-effort maintenance ended March 2026 (as of 2025-11-11 announcement)
- Gateway API: v1.6.2 latest; TCPRoute/UDPRoute GA in v1.6 (as of 2026 (year inferred))
- K3s: v1.36.x (v1.36.0+k3s1 2026-05-06; etcd 3.6, Traefik v3.6) (as of 2026-09)
- Talos Linux: v1.14.1 (K8s 1.37.0); Sidero acquired by Yardi; hypervisor GA planned Dec 2026 (as of 2026-09-15)
- Flux: v2.9.5 (2.9.0 on 2026-06-30; requires K8s 1.34+) (as of 2026-08-31)
- KubeEdge: v1.23.1; CNCF graduated 2024-10-15 (as of 2026-07-15)
- AWS Snowball Edge: Closed to new customers (as of 2025-11-07)
- AWS IoT Greengrass V1: End of support; resources inaccessible afterwards (as of 2026-10-07)
- AWS IoT Greengrass v2 nucleus: 2.18.0 (2026-07-08); nucleus lite 2.5.1 (2026-05-06) (as of 2026-07-15)
- AWS Outposts: Gen-2 racks GA 2025-04-29; single-rack gen-2 GA 2026-09-10; 1U/2U servers discontinued (as of 2026-09-10)
- EKS Hybrid Nodes: GA (re:Invent Dec 2024); per vCPU-hour; not for DDIL (as of 2026-09)
- AWS Interconnect - multicloud: GA with Google Cloud and OCI; Azure in preview (as of 2026-09)
- Azure Local: Renamed from Azure Stack HCI 2024-11-19; monthly builds (2609); disconnected operations from 2602 (as of 2026-09)
- Azure IoT Operations: 1.4.x current (1.4.73, 2608); 1.3.x and 1.2.x supported (as of 2026-06)
- Azure IoT Edge: 1.6 LTS (July 2026, to 2028-11-14); 1.5 LTS ends 2026-11-10 (as of 2026-07-17)
- Google Distributed Cloud: Connected + air-gapped; Gemini Flash preview on GDC connected (Next '26) (as of 2026-04-22)
- NVIDIA Jetson AGX Thor: GA; dev kit $3,499; JetPack 7.2.1 current (as of 2025-08-25 / 2026-08-12)
- TensorRT Edge-LLM: v0.10.1 (as of 2026-09-03)
- ExecuTorch: 1.0 GA 2025-10-17; 1.5.x current (as of 2026-09 (year inferred))
- Mosquitto: 2.1.2 (2.1.0 on 2026-01-29) (as of 2026-02-09)
- EMQX: BSL 1.1 from 5.9 (2025-05-07); 6.3 LTS current (as of 2026-09 (6.3 year inferred))
- WASI: 0.3.0 released (native async) (as of 2026-06-11)
- RFC 10024 hybrid ML-KEM for TLS 1.3: Published (Standards Track) (as of 2026-08)
- OpenSSL: 3.5 LTS (EOL 2030-04-08); 4.0.2 (EOL 2027-05-14); 4.1.0-beta1 (2026-09-23) (as of 2026-09-23)
- curl: 8.22.0 (as of 2026-09-02)
- IPv6 (Google measurement): Exceeded 50% (50.10%) (as of 2026-03-28)
STALE-KNOWLEDGE TRAPS:
- Recommending Snowball Edge or Snowcone for tactical edge: Snowcone was discontinued Nov 2024 and Snowball Edge closed to new customers Nov 7, 2025.
- Describing Outposts 1U/2U servers as available: sales are discontinued. The small form factor is now the 42U single-rack gen-2 Outposts (Sept 2026).
- Treating Greengrass V1 as usable: support ends Oct 7, 2026.
- Positioning EKS Hybrid Nodes for DDIL or disconnected sites: AWS says to use EKS Anywhere for those. Hybrid Nodes is also billed per vCPU-hour.
- Using the names Azure Stack HCI or AKS on HCI: the product is Azure Local (with AKS enabled by Arc) and has disconnected operations from build 2602.
- Treating Azure IoT Edge as the strategic Azure edge platform: Azure IoT Operations on Arc is. IoT Edge 1.5 LTS ends Nov 10, 2026, and 1.6 LTS is current.
- Calling Google's on-prem product Anthos or GKE on-prem: it is Google Distributed Cloud (connected/air-gapped), and Gemini now runs on it.
- Teaching ingress-nginx as the default ingress: it is retired with no fixes after March 2026. Teach Gateway API v1.6.
- Assuming Linkerd stable releases are open source: since Feb 2024 open source ships edge releases only, and stable builds come from Buoyant Enterprise for Linkerd.
- Calling Istio ambient beta: it has been GA since 1.24 (Nov 2024), and 1.31 is current.
- Assuming EMQX open source is Apache 2.0 with free clustering: 5.9+ is BSL 1.1 and clustering needs a license.
- Using TensorFlow Lite: LiteRT is the successor and TFLite is maintenance-only. Calling ExecuTorch beta: it has been GA since Oct 2025.
- Assuming Orin means JetPack 6: JetPack 7.2 covers Orin, Jetson Thor is GA, and TensorRT Edge-LLM is NVIDIA's embedded LLM runtime.
- Teaching WASI Preview 2 async via wasi:io pollables: WASI 0.3 (June 2026) moved async into the Component Model and removed wasi:io.
- Calling post-quantum TLS future work: X25519MLKEM768 is the default in browsers and OpenSSL 3.5+, is standardized in RFC 10024 (Aug 2026), and its larger ClientHello can break middleboxes and hit MTU problems.
- Using OpenSSL 3.0 as the baseline LTS: 3.5 is the LTS, 4.0 is released, and 3.4/3.6 reach EOL Oct/Nov 2026.
- Assuming curl has NTLM on and supports OpenSSL-QUIC: 8.20 disabled NTLM by default and dropped OpenSSL-QUIC, and 8.22 dropped TLS-SRP.
- Assuming K3s HA upgrades can be rolled back: etcd 3.6 in K3s 1.36 blocks downgrade. MicroK8s 1.36 also removed the Dashboard and changed GPU runtime defaults.
- Calling IPv6 a small minority: Google passed 50% in Mar 2026, though aggregate measures are about 43%.
- Assuming cross-cloud private links require a colocation provider: managed AWS Interconnect - multicloud and Google Partner Cross-Cloud Interconnect now exist.
- Treating Talos as a small independent vendor product and Fermyon as an independent startup: Yardi acquired Sidero Labs, and Akamai acquired Fermyon.

## RN09-learning-resources-and-case-studies
VERSIONS/STATUS:
- Designing Data-Intensive Applications 2nd ed. (Kleppmann, Riccomini): Published (as of 2026-03)
- Observability Engineering 2nd ed.: Published / announced; free with sign-up from Honeycomb (as of 2026-06-17)
- AI Engineering (Chip Huyen): Current edition (1st) (as of 2025-01)
- Computer Networking: A Top-Down Approach 9th ed.: Current (as of Summer 2025)
- Security Engineering 3rd ed. (Ross Anderson): Fully free to download (as of 2024-11)
- Terraform: Up & Running 3rd ed.: Current; no 4th ed. (as of 2022-09-26)
- Fundamentals of DevOps and Software Delivery (Brikman): Published (as of 2025-05-20)
- Effective TypeScript 2nd ed.: Current (as of 2024-05)
- Learning Go 2nd ed.: Current (as of 2024-01)
- DORA software delivery metrics: Five metrics; MTTR renamed failed deployment recovery time; rework rate added (as of 2026-01-05)
- Google Skills (successor to Cloud Skills Boost): GA / launched (as of 2025-10)
- AWS Free Tier credit model: Effective for new accounts (as of 2025-07-15)
- Microsoft Learn Azure sandboxes: Retired (as of 2026)
- Kubernetes The Hard Way: Kubernetes v1.32.x / containerd v2.1.x / etcd v3.6.x / CNI v1.6.x (as of 2026-09)
- OpenTelemetry Demo: 3.1.0 latest (3.0.0 added Agent/MCP/Chatbot services) (as of 2026-09-18 (year implied))
- Google Online Boutique: v0.10.7 latest (as of 2026-09-18 (year implied))
- LitmusChaos: CNCF Incubating; 3.32.0 latest (as of 2026-09-17 (year implied))
- Chaos Mesh: CNCF Incubating since 2022-02-16; v2.8.4 latest (as of 2026-08-18 (year implied))
- CNPE certification: Available; $445; 2-hour performance-based; killer.sh included (as of 2026-09)
- iximiuz Labs Complete Bundle: $10/mo annual (promo $6), $299 lifetime; free tier 1 hour/day (as of 2026-09)
- AWS us-east-1 DynamoDB DNS outage PES: Published (as of 2025-10-19/20)
- Google Cloud Service Control outage: Incident report published (as of 2025-06-12)
- Azure Front Door outage PIR (YKYN-BWZ): Published (as of 2025-10-29)
- Cloudflare outage (Bot Management feature file): Postmortem published (as of 2025-11-18)
- Cloudflare outage (WAF config / FL1 Lua): Postmortem published (as of 2025-12-05)
- Cloudflare BYOIP outage: Postmortem published (as of 2026-02-20)
- Cloudflare Code Orange: Fail Small: Completed (as of 2026-05-01)
- CrowdStrike Channel File 291 RCA: Published (as of 2024-08-06)
- DIU Thunderforge (Scale AI prime): Awarded (as of 2025-03-05)
- Databricks Agent Bricks: Beta at launch (as of 2025-06-11)
- Pragmatic Engineer FDE deep-dive: Published (as of 2025-08-12)
- MADR: 4.0.0 (as of 2024-09-17)
- Google developer documentation style guide: Last updated (as of 2026-04-27)
STALE-KNOWLEDGE TRAPS:
- DDIA 1st ed. (2017) is superseded by the 2nd ed. (March 2026, with Chris Riccomini)
- Observability Engineering 1st ed. (2022) is superseded by the 2nd ed. (June/July 2026; new co-authors, 27 new chapters, LLM observability)
- Kurose-Ross 8th ed. is superseded by the 9th ed. (summer 2025)
- Security Engineering 3e is now fully free (since November 2024), not sample chapters; Ross Anderson died March 2024
- DORA 'four keys' is outdated: there are five metrics, MTTR became failed deployment recovery time, and deployment rework rate was added
- Microsoft Learn free Azure sandboxes are retired; learners need their own subscription
- Google Cloud Skills Boost is now Google Skills (October 2025), credits-based
- The AWS 12-month free tier is replaced for new accounts (from 2025-07-15) by up to $200 in credits and a 6-month Free Plan
- workshops.aws redirects to AWS Builder Center
- Kubernetes The Hard Way is no longer GCP-specific: Kubernetes 1.32.x on four Debian 12 ARM64/AMD64 machines, containerd 2.x
- OpenTelemetry Demo 3.x renamed attributes app.* to demo.* and added Agent, Chatbot and MCP services; the load generator is back on Locust
- A new CNCF CNPE certification exists, with killer.sh and Killercoda coverage
- Canonical outage case studies should be updated beyond S3 2017 / Facebook 2021 to the 2025-2026 set (Google Service Control, AWS DynamoDB DNS, Azure Front Door, Cloudflare Nov/Dec 2025 and Feb 2026)
- k8s.af is now hosted on Codeberg
- Anthropic customer stories moved to claude.com/customers
- Palantir Agent Studio is now AIP Chatbot Studio; a free AIP Developer Tier exists
- Scale AI: Meta took a 49% stake (June 2025), Alexandr Wang left, and Jason Droege is CEO
- Terraform: Up & Running 3e (2022) predates OpenTofu; Brikman's 2025 'Fundamentals of DevOps and Software Delivery' is the newer text
- iximiuz Labs is freemium, with a 1 hour/day free cap
- MADR is at 4.0.0 (September 2024) with bare and minimal templates
- TryHackMe added a MAX tier; Hack The Box launched HTB PRO (2026-09-21) and an AI Range
- CodeCrafters now has a 'build your own Claude Code' challenge




