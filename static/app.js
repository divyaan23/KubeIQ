function text(id, value) {
  const el = document.getElementById(id);
  if (el) el.textContent = value;
}

let portalGenerateTargets = [];
let tokenomicsTarget = null;

function reasoningTitle(classification) {
  return classification && classification.reasoning ? classification.reasoning : "No reasoning returned";
}

function confidencePct(classification) {
  const confidence = classification && typeof classification.confidence === "number" ? classification.confidence : 0;
  return Math.round(confidence * 100);
}

function renderSummary(workloads) {
  const total = workloads.length;
  const aiWorkloads = workloads.filter((w) => w.ai_classification && w.ai_classification.is_ai_workload);
  const aiConfirmed = aiWorkloads.length;
  const missing = workloads.filter((w) => w.policy === "Missing");
  const unmanaged = missing.length;
  const gpuTotal = workloads.reduce((sum, w) => sum + (w.gpu_requested || 0), 0);

  text("summary-total", total);
  text("summary-ai", aiConfirmed);
  text("summary-unmanaged", unmanaged);
  text("summary-gpu", gpuTotal);

  text("portal-discovery", total ? `${total} workload${total === 1 ? "" : "s"} scanned` : "No workloads found yet");
  text(
    "portal-recommendations",
    unmanaged
      ? `${unmanaged} workload${unmanaged === 1 ? "" : "s"} need a policy`
      : "All workloads governed"
  );

  const recommendationsDomain = document.querySelector(".domain-recommendations");
  if (recommendationsDomain) {
    recommendationsDomain.classList.toggle("needs-attention", unmanaged > 0);
  }

  // Missing-policy workloads take priority, but a governed workload is still a
  // valid target so the button/icon is never a dead end once data has loaded.
  portalGenerateTargets = missing.length ? missing : workloads;
  const generateButton = document.getElementById("portal-generate-button");
  if (generateButton) {
    generateButton.hidden = total === 0;
  }

  tokenomicsTarget = aiWorkloads[0] || workloads[0] || null;
  text(
    "portal-tokenomics",
    tokenomicsTarget ? `${tokenomicsTarget.namespace}/${tokenomicsTarget.workload}` : "No workloads found yet"
  );
}

function renderInventory(workloads) {
  const body = document.getElementById("inventory");
  if (!workloads.length) {
    body.innerHTML = '<tr><td colspan="9" class="empty">No workloads discovered yet.</td></tr>';
    return;
  }

  body.replaceChildren(
    ...workloads.map((workload) => {
      const row = document.createElement("tr");
      const classification = workload.ai_classification || {};
      const isAi = classification.is_ai_workload;
      const policyMissing = workload.policy === "Missing";

      row.innerHTML = `
        <td>${workload.namespace}</td>
        <td>${workload.workload}</td>
        <td class="image-cell" title="${workload.images.join(', ')}">${workload.images[0] || "unknown"}</td>
        <td><span class="badge ${isAi ? 'badge-yes' : 'badge-no'}">${isAi ? "Yes" : "No"}</span></td>
        <td>${classification.provider || "--"}</td>
        <td title="${classification.source === 'runtime_observed' ? 'Confirmed from live sidecar traffic' : 'Inferred from Kubernetes manifest evidence only'}">${classification.model || "--"}${classification.source === 'runtime_observed' ? ' <span class="badge badge-ok">observed</span>' : ''}</td>
        <td title="${reasoningTitle(classification)}">${confidencePct(classification)}%</td>
        <td>${workload.gpu_requested || 0}</td>
        <td><span class="badge ${policyMissing ? 'badge-missing' : 'badge-ok'}">${workload.policy}</span></td>
      `;
      return row;
    })
  );
}

let activeRecommendation = { namespace: null, workload: null, policyName: null };

function positionPopoverNear(popover, anchor) {
  const anchorRect = anchor.getBoundingClientRect();
  const margin = 12;
  const popoverWidth = popover.getBoundingClientRect().width || Math.min(420, window.innerWidth * 0.92);
  let left = anchorRect.left;

  if (left + popoverWidth > window.innerWidth - margin) {
    left = window.innerWidth - popoverWidth - margin;
  }
  popover.style.left = `${Math.max(margin, left)}px`;

  // Pick whichever side of the anchor has more room, then cap max-height to
  // what's actually visible there so Copy/Download never end up off-screen
  // behind a scroll area the user can't reach.
  const spaceBelow = window.innerHeight - anchorRect.bottom - margin;
  const spaceAbove = anchorRect.top - margin;
  const preferBelow = spaceBelow >= 240 || spaceBelow >= spaceAbove;

  if (preferBelow) {
    popover.style.top = `${anchorRect.bottom + 8}px`;
    popover.style.maxHeight = `${Math.max(160, spaceBelow - 8)}px`;
  } else {
    const height = Math.min(spaceAbove, window.innerHeight * 0.8);
    popover.style.top = `${Math.max(margin, anchorRect.top - height)}px`;
    popover.style.maxHeight = `${Math.max(160, height)}px`;
  }
}

function openPolicyRecommendation(namespace, workload, anchor) {
  activeRecommendation = { namespace, workload, policyName: `${workload}-policy` };
  text("policy-modal-title", `Recommended policy \u00b7 ${namespace}/${workload}`);
  document.getElementById("policy-guidance").value = "";
  document.getElementById("policy-modal-yaml").textContent = "Click Generate to create a recommendation.";
  document.getElementById("policy-modal-rationale").innerHTML = "";
  const popover = document.getElementById("policy-modal");
  popover.hidden = false;
  positionPopoverNear(popover, anchor);
}

document.addEventListener("click", (event) => {
  ["policy-modal", "governance-panel", "tokenomics-panel", "gpu-panel"].forEach((id) => {
    const popover = document.getElementById(id);
    if (popover && !popover.hidden && !popover.contains(event.target)) {
      popover.hidden = true;
    }
  });
});

// Sample-only feature (no GPU nodes in this cluster yet) — content is static,
// so opening it just needs to position and reveal the popover, no fetch.
function openGpuPanel(anchor) {
  const popover = document.getElementById("gpu-panel");
  popover.hidden = false;
  positionPopoverNear(popover, anchor);
}

document.getElementById("gpu-close").addEventListener("click", () => {
  document.getElementById("gpu-panel").hidden = true;
});

const portalGpuIcon = document.getElementById("portal-gpu-icon");
if (portalGpuIcon) {
  portalGpuIcon.addEventListener("click", (event) => {
    event.stopPropagation();
    openGpuPanel(portalGpuIcon);
  });
}

function openPortalRecommendation(anchor) {
  const target = portalGenerateTargets[0];
  if (!target) return;
  openPolicyRecommendation(target.namespace, target.workload, anchor);
}

const portalGenerateButton = document.getElementById("portal-generate-button");
if (portalGenerateButton) {
  portalGenerateButton.addEventListener("click", (event) => {
    event.stopPropagation();
    openPortalRecommendation(portalGenerateButton);
  });
}

function renderGovernancePanel(data) {
  const posture = data.posture || { total: 0, governed: 0, missing: 0 };
  text("governance-count", data.standards.length);
  text("governance-governed", posture.governed);
  text("governance-missing", posture.missing);
  text(
    "governance-title",
    posture.missing ? `${posture.missing} workload${posture.missing === 1 ? "" : "s"} need a policy` : "All scanned workloads governed"
  );

  const container = document.getElementById("governance-standards");
  if (!data.standards.length) {
    container.innerHTML = '<p class="policy-modal-note">No AIPolicy resources found in the cluster yet.</p>';
    return;
  }

  container.replaceChildren(
    ...data.standards.map((standard) => {
      const card = document.createElement("article");
      card.className = "governance-standard";
      const scope = Object.entries(standard.scope || {}).map(([key, value]) => `${key}=${value}`).join(", ") || "all workloads";
      const tokenLimits = `${standard.max_input_tokens ?? "None"} input / ${standard.max_output_tokens ?? "None"} output`;
      const gpuTypes = standard.allowed_gpu_types && standard.allowed_gpu_types.length ? ` \u00b7 ${standard.allowed_gpu_types.join(", ")}` : "";
      const governedWorkloads = standard.governed_workloads || [];
      const violations = standard.violations || [];

      const workloadRows = governedWorkloads.length
        ? governedWorkloads.map((w) => {
          const workloadViolations = violations.filter((v) => v.namespace === w.namespace && v.workload === w.workload);
          const violationCell = workloadViolations.length
            ? `<span class="badge badge-missing">${workloadViolations.length}</span> ${workloadViolations[0].decision || ""} \u00b7 ${workloadViolations[0].violation_reason || "Unknown reason"}`
            : '<span class="badge badge-ok">0</span> None in the observed window';
          return `
            <tr>
              <td>${w.namespace}</td>
              <td>${w.workload}</td>
              <td>${violationCell}</td>
            </tr>
          `;
        }).join("")
        : '<tr><td colspan="3" class="empty">No workloads matched this policy yet.</td></tr>';

      const workloadTable = `
        <table class="governance-workload-table">
          <thead><tr><th>Namespace</th><th>Workload</th><th>Policy Violation</th></tr></thead>
          <tbody>${workloadRows}</tbody>
        </table>
      `;
      card.innerHTML = `
        <div class="governance-standard-heading">
          <strong>${standard.name} \u00b7 v${standard.generation}</strong>
          <span class="${standard.enforcement_active ? 'mode-enforce' : 'mode-observe'}">${standard.enforcement_active ? 'ENFORCE' : 'OBSERVE'}</span>
        </div>
        <p>Scope: ${standard.namespace} \u00b7 ${scope}</p>
        <p>Registries: ${(standard.allowed_registries || []).join(', ') || 'Any'}</p>
        <p>Providers: ${(standard.allowed_providers || []).join(', ') || 'Any'} \u00b7 Models: ${(standard.allowed_models || []).join(', ') || 'Any'}</p>
        <p>Token limits: ${tokenLimits}</p>
        <p>GPU: ${standard.max_gpus == null ? 'No limit' : `Up to ${standard.max_gpus}`}${gpuTypes}</p>
        <p>Runtime sidecar: ${standard.require_sidecar ? 'Required' : 'Optional'}</p>
        <p class="governance-workload-heading">Workloads governed (${governedWorkloads.length})</p>
        ${workloadTable}
      `;
      return card;
    })
  );
}

async function openGovernancePanel(anchor) {
  const popover = document.getElementById("governance-panel");
  popover.hidden = false;
  positionPopoverNear(popover, anchor);
  text("governance-title", "Loading policy governance...");
  try {
    const response = await fetch("/api/policy-governance");
    if (!response.ok) throw new Error("request failed");
    renderGovernancePanel(await response.json());
  } catch (error) {
    text("governance-title", "Could not load policy governance");
  }
}

document.getElementById("governance-close").addEventListener("click", () => {
  document.getElementById("governance-panel").hidden = true;
});

const portalRecommendationsIcon = document.getElementById("portal-recommendations-icon");
if (portalRecommendationsIcon) {
  portalRecommendationsIcon.addEventListener("click", (event) => {
    event.stopPropagation();
    openGovernancePanel(portalRecommendationsIcon);
  });
}

function renderTokenomicsPanel(data) {
  const stats = data.token_stats;
  const successful = (stats && stats.successful) || null;
  const blockedRequests = (stats && stats.blocked_requests) || [];
  const requestHistory = (stats && stats.request_history) || [];

  text("tokenomics-workload", data.workload || "--");
  text("tokenomics-namespace", data.namespace || "--");
  text("tokenomics-generated-at", data.generated_at ? new Date(data.generated_at).toLocaleString() : "--");
  text("tokenomics-blocked", blockedRequests.length);

  const historyBody = document.getElementById("tokenomics-history");
  if (requestHistory.length) {
    historyBody.replaceChildren(
      ...requestHistory.map((item) => {
        const row = document.createElement("tr");
        const time = item.observed_at ? new Date(item.observed_at).toLocaleString() : "--";
        const status = item.status_code === 403
          ? `<span class="mode-enforce">${item.violation_label || item.violation_reason || "Blocked"}</span>`
          : `<span class="mode-observe">Successful</span>`;
        row.innerHTML = `
          <td>${time}</td>
          <td>${item.workload || data.workload}</td>
          <td>${item.namespace || data.namespace}</td>
          <td>${item.input_tokens ?? "--"}</td>
          <td>${item.output_tokens ?? "--"}</td>
          <td>${status}</td>
        `;
        return row;
      })
    );
  } else {
    historyBody.innerHTML = '<tr><td colspan="6" class="empty">No requests recorded in this window.</td></tr>';
  }

  if (data.cold_start) {
    text("tokenomics-title", `Tokenomics \u00b7 ${data.namespace}/${data.workload}`);
    text("tokenomics-avg-input", "--");
    text("tokenomics-avg-output", "--");
    text("tokenomics-requests", "0");
    document.getElementById("tokenomics-distribution").innerHTML = "";
    document.getElementById("tokenomics-recommendation").innerHTML =
      '<p class="policy-modal-note">No observed traffic for this workload yet \u2014 the agent needs real request data before it can recommend token limits.</p>';
    return;
  }

  const input = successful.input || {};
  const output = successful.output || {};
  text("tokenomics-title", `Tokenomics \u00b7 ${data.namespace}/${data.workload}`);
  text("tokenomics-avg-input", successful.requests_analyzed ? (input.average ?? "--") : "--");
  text("tokenomics-avg-output", successful.requests_analyzed ? (output.average ?? "--") : "--");
  text("tokenomics-requests", successful.requests_analyzed ?? 0);

  const distribution = document.getElementById("tokenomics-distribution");
  if (successful.requests_analyzed) {
    const distCard = document.createElement("article");
    distCard.className = "governance-standard";
    distCard.innerHTML = `
      <div class="governance-standard-heading"><strong>Observed distribution (7-day)</strong></div>
      <p>P50: ${input.p50 ?? '--'} in \u00b7 ${output.p50 ?? '--'} out</p>
      <p>P95: ${input.p95 ?? '--'} in \u00b7 ${output.p95 ?? '--'} out</p>
      <p>P99: ${input.p99 ?? '--'} in \u00b7 ${output.p99 ?? '--'} out</p>
      <p>Max: ${input.max ?? '--'} in \u00b7 ${output.max ?? '--'} out</p>
    `;
    distribution.replaceChildren(distCard);
  } else {
    distribution.innerHTML = '<p class="policy-modal-note">No successful requests in this window \u2014 recommendation is based on blocked attempts only.</p>';
  }

  const recommendationContainer = document.getElementById("tokenomics-recommendation");
  const recommendation = data.recommendation;
  if (!recommendation) {
    recommendationContainer.innerHTML = '<p class="policy-modal-note">Agent recommendation unavailable right now \u2014 try again shortly.</p>';
    return;
  }

  const card = document.createElement("article");
  card.className = "governance-standard";
  const confidencePercent = Math.round((recommendation.confidence || 0) * 100);
  const pricing = data.pricing || {};
  const pricingLine = pricing.source
    ? `<p>Pricing: $${pricing.input_per_million_usd}/$${pricing.output_per_million_usd} per 1M tokens (${pricing.source === "policy" ? "policy-configured" : "reference estimate \u2014 verify against your actual rate"})</p>`
    : "";
  card.innerHTML = `
    <div class="governance-standard-heading">
      <strong>Agent recommendation</strong>
      <span class="mode-observe">${confidencePercent}% confidence</span>
    </div>
    <p>Suggested input limit: ${recommendation.suggested_max_input_tokens ?? 'No change suggested'}</p>
    <p>Suggested output limit: ${recommendation.suggested_max_output_tokens ?? 'No change suggested'}</p>
    ${pricingLine}
  `;
  const rationaleList = document.createElement("ul");
  (recommendation.rationale || []).forEach((item) => {
    const li = document.createElement("li");
    li.textContent = item;
    rationaleList.appendChild(li);
  });
  card.appendChild(rationaleList);
  recommendationContainer.replaceChildren(card);
}

async function openTokenomicsPanel(anchor) {
  const target = tokenomicsTarget;
  const popover = document.getElementById("tokenomics-panel");
  popover.hidden = false;
  positionPopoverNear(popover, anchor);

  if (!target) {
    text("tokenomics-title", "No workloads scanned yet");
    return;
  }

  text("tokenomics-title", "Loading tokenomics...");
  try {
    const params = new URLSearchParams({ namespace: target.namespace, workload: target.workload });
    const response = await fetch(`/api/token-recommendation?${params.toString()}`);
    if (!response.ok) throw new Error("request failed");
    renderTokenomicsPanel(await response.json());
  } catch (error) {
    text("tokenomics-title", "Could not load tokenomics");
  }
}

document.getElementById("tokenomics-close").addEventListener("click", () => {
  document.getElementById("tokenomics-panel").hidden = true;
});

const portalTokenomicsIcon = document.getElementById("portal-tokenomics-icon");
if (portalTokenomicsIcon) {
  portalTokenomicsIcon.addEventListener("click", (event) => {
    event.stopPropagation();
    openTokenomicsPanel(portalTokenomicsIcon);
  });
}

async function generatePolicyRecommendation() {
  const { namespace, workload } = activeRecommendation;
  if (!namespace || !workload) return;

  const yamlEl = document.getElementById("policy-modal-yaml");
  const rationaleEl = document.getElementById("policy-modal-rationale");
  const guidance = document.getElementById("policy-guidance").value.trim();
  yamlEl.textContent = "Generating recommendation...";
  rationaleEl.innerHTML = "";

  try {
    const params = new URLSearchParams({ namespace, workload });
    if (guidance) params.set("guidance", guidance);
    const response = await fetch(`/api/policy-recommendations?${params.toString()}`);
    if (!response.ok) throw new Error("request failed");
    const data = await response.json();
    activeRecommendation.policyName = data.policy_name || activeRecommendation.policyName;
    yamlEl.textContent = data.policy_yaml;
    rationaleEl.replaceChildren(
      ...data.rationale.map((item) => {
        const li = document.createElement("li");
        li.textContent = item;
        return li;
      })
    );
  } catch (error) {
    yamlEl.textContent = "Could not generate a recommendation for this workload.";
  }
}

document.getElementById("policy-generate-button").addEventListener("click", generatePolicyRecommendation);

document.getElementById("policy-modal-close").addEventListener("click", () => {
  document.getElementById("policy-modal").hidden = true;
});

document.getElementById("policy-modal-copy").addEventListener("click", async (event) => {
  const yamlText = document.getElementById("policy-modal-yaml").textContent;
  const button = event.currentTarget;
  const originalLabel = button.textContent;

  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(yamlText);
    } else {
      // navigator.clipboard is unavailable on plain HTTP origins; fall back to
      // the legacy selection-based copy so this still works over http://.
      const textarea = document.createElement("textarea");
      textarea.value = yamlText;
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.focus();
      textarea.select();
      document.execCommand("copy");
      document.body.removeChild(textarea);
    }
    button.textContent = "Copied!";
  } catch (error) {
    button.textContent = "Copy failed";
  } finally {
    setTimeout(() => {
      button.textContent = originalLabel;
    }, 1500);
  }
});

document.getElementById("policy-modal-download").addEventListener("click", () => {
  const yamlText = document.getElementById("policy-modal-yaml").textContent;
  const filename = `${activeRecommendation.policyName || "aipolicy"}.yaml`;
  const blob = new Blob([yamlText], { type: "application/yaml" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  // Some browsers (notably Safari) only honor `download` on an anchor that's
  // actually attached to the document when .click() is called.
  document.body.appendChild(link);
  link.click();
  document.body.removeChild(link);
  URL.revokeObjectURL(url);
});

async function loadWorkloads(refresh) {
  text("last-updated", refresh ? "Rescanning with agent..." : "Loading...");
  try {
    const response = await fetch(`/api/workloads${refresh ? "?refresh=true" : ""}`);
    const data = await response.json();
    renderSummary(data.workloads);
    renderInventory(data.workloads);
    text("last-updated", `Updated ${new Date().toLocaleTimeString()}`);
    openRequestedRecommendation();
  } catch (error) {
    text("last-updated", "Failed to load workloads");
  }
}

// Lets the telemetry dashboard deep-link straight into this workload's
// generator via ?namespace=...&workload=..., instead of only linking here.
let autoOpenHandled = false;
function openRequestedRecommendation() {
  if (autoOpenHandled) return;
  const params = new URLSearchParams(window.location.search);
  const namespace = params.get("namespace");
  const workload = params.get("workload");
  if (!namespace || !workload) return;

  const anchor = document.getElementById("portal-generate-button") || document.getElementById("portal-recommendations-icon");
  if (!anchor) return;

  autoOpenHandled = true;
  openPolicyRecommendation(namespace, workload, anchor);
}

document.getElementById("refresh-button").addEventListener("click", () => loadWorkloads(true));
loadWorkloads(false);
if (window.lucide) window.lucide.createIcons();
