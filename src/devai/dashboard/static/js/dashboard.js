/**
 * DevAI Dashboard — Client-side application
 * Handles: OAuth, Kanban board, pipeline status, permissions, CLAUDE.md governance
 */

const API = '/dashboard/api';

// --- State ---
const state = {
  user: null,
  orgs: [],
  selectedOrg: null,
  repos: [],
  selectedRepo: null,
  projects: [],
  selectedProject: null,
  board: { columns: {} },
  runs: [],
  activeTab: 'board',
  permissions: {
    autoMode: false,     // true = no gates, auto-continue full epic
    gates: {
      deployment: true,  // ask before deploy
      testing: true,     // ask before running tests
      review: false,     // ask before submitting review
      merge: true,       // ask before merge
      createPR: false,   // ask before creating PR
    },
  },
  claudeMd: '',          // CLAUDE.md governance content
  pendingApprovals: [],   // queued approval requests
};

// --- Init ---
document.addEventListener('DOMContentLoaded', async () => {
  await checkAuth();
  render();
  if (state.user) {
    await loadOrgs();
    startPolling();
  }
});

// --- Auth ---
async function checkAuth() {
  try {
    const resp = await fetch(`${API}/me`);
    if (resp.ok) {
      state.user = await resp.json();
    }
  } catch (e) { /* not logged in */ }
}

function login() {
  window.location.href = '/dashboard/auth/login';
}

function logout() {
  window.location.href = '/dashboard/auth/logout';
}

// --- Data Loading ---
async function loadOrgs() {
  const resp = await fetch(`${API}/orgs`);
  if (resp.ok) state.orgs = await resp.json();
  render();
}

async function selectOrg(org) {
  state.selectedOrg = org;
  state.selectedProject = null;
  state.board = { columns: {} };

  const [reposResp, projectsResp] = await Promise.all([
    fetch(`${API}/orgs/${org}/repos`),
    fetch(`${API}/orgs/${org}/projects`),
  ]);

  if (reposResp.ok) state.repos = await reposResp.json();
  if (projectsResp.ok) state.projects = await projectsResp.json();
  render();
}

async function selectProject(projectNumber) {
  state.selectedProject = state.projects.find(p => p.number === projectNumber) || null;
  if (state.selectedProject) {
    await loadBoard();
  }
  render();
}

async function loadBoard() {
  if (!state.selectedOrg || !state.selectedProject) return;
  const resp = await fetch(`${API}/orgs/${state.selectedOrg}/projects/${state.selectedProject.number}/board`);
  if (resp.ok) state.board = await resp.json();
  render();
}

async function loadRuns() {
  const params = state.selectedRepo ? `?repo=${state.selectedRepo}` : '';
  const resp = await fetch(`${API}/pipeline/runs${params}`);
  if (resp.ok) state.runs = await resp.json();
  if (state.activeTab === 'pipeline') render();
}

// --- Polling ---
let pollInterval = null;
function startPolling() {
  pollInterval = setInterval(() => {
    if (state.activeTab === 'board' && state.selectedProject) loadBoard();
    if (state.activeTab === 'pipeline') loadRuns();
  }, 10000);
}

// --- Actions ---
async function createProject() {
  const title = prompt('Project name:', 'DevAI Pipeline Board');
  if (!title) return;
  const resp = await fetch(`${API}/orgs/${state.selectedOrg}/projects`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title }),
  });
  if (resp.ok) {
    showToast('Project created', 'success');
    const projectsResp = await fetch(`${API}/orgs/${state.selectedOrg}/projects`);
    if (projectsResp.ok) state.projects = await projectsResp.json();
    render();
  }
}

async function moveItem(itemId, targetStatus) {
  const resp = await fetch(`${API}/orgs/${state.selectedOrg}/projects/${state.selectedProject.number}/move`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ item_id: itemId, target_status: targetStatus }),
  });
  if (resp.ok) {
    showToast(`Moved to ${targetStatus}`, 'success');
    await loadBoard();
  }
}

async function triggerPipeline(issueNumber) {
  if (!state.selectedRepo) {
    showToast('Select a repository first', 'warning');
    return;
  }

  const resp = await fetch(`${API}/pipeline/trigger`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      repo: state.selectedRepo,
      issue_number: issueNumber,
      permissions: state.permissions,
      claude_md: state.claudeMd,
    }),
  });

  if (resp.ok) {
    const data = await resp.json();
    showToast(`Pipeline triggered: ${data.run_id}`, 'success');
    switchTab('pipeline');
    await loadRuns();
  }
}

async function triggerFromRequirements() {
  const req = document.getElementById('requirements-input')?.value;
  if (!req || !state.selectedRepo) return;

  const resp = await fetch(`${API}/pipeline/trigger`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      repo: state.selectedRepo,
      requirements: req,
      permissions: state.permissions,
      claude_md: state.claudeMd,
    }),
  });

  if (resp.ok) {
    const data = await resp.json();
    showToast(`Pipeline triggered: ${data.run_id}`, 'success');
    closeDialog();
    switchTab('pipeline');
    await loadRuns();
  }
}

// --- Permission / Approval ---
function toggleAutoMode() {
  state.permissions.autoMode = !state.permissions.autoMode;
  render();
}

function toggleGate(gate) {
  state.permissions.gates[gate] = !state.permissions.gates[gate];
  render();
}

async function approveGate(runId, gate) {
  state.pendingApprovals = state.pendingApprovals.filter(
    a => !(a.runId === runId && a.gate === gate)
  );
  // In production, this would call the API to release the gate
  showToast(`Approved: ${gate} for ${runId.slice(0, 8)}`, 'success');
  render();
}

async function rejectGate(runId, gate) {
  state.pendingApprovals = state.pendingApprovals.filter(
    a => !(a.runId === runId && a.gate === gate)
  );
  showToast(`Rejected: ${gate} for ${runId.slice(0, 8)}`, 'error');
  render();
}

// --- CLAUDE.md Governance ---
function updateClaudeMd() {
  const textarea = document.getElementById('claude-md-editor');
  if (textarea) state.claudeMd = textarea.value;
}

async function saveClaudeMd() {
  if (!state.selectedRepo) {
    showToast('Select a repository first', 'warning');
    return;
  }
  const resp = await fetch(`${API}/governance/claude-md`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repo: state.selectedRepo, content: state.claudeMd }),
  });
  if (resp.ok) {
    showToast('CLAUDE.md governance saved', 'success');
  }
}

async function loadClaudeMd() {
  if (!state.selectedRepo) return;
  try {
    const resp = await fetch(`${API}/governance/claude-md?repo=${state.selectedRepo}`);
    if (resp.ok) {
      const data = await resp.json();
      state.claudeMd = data.content || '';
      render();
    }
  } catch (e) { /* no file yet */ }
}

// --- UI ---
function switchTab(tab) {
  state.activeTab = tab;
  if (tab === 'pipeline') loadRuns();
  render();
}

function showToast(msg, type = 'info') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = `toast toast-${type} show`;
  setTimeout(() => el.className = 'toast', 3000);
}

let dialogOpen = false;
function openDialog(id) {
  document.getElementById(id).classList.add('active');
  dialogOpen = true;
}
function closeDialog() {
  document.querySelectorAll('.dialog-overlay').forEach(el => el.classList.remove('active'));
  dialogOpen = false;
}

// --- Render ---
function render() {
  const app = document.getElementById('app');
  if (!state.user) {
    app.innerHTML = renderLogin();
    return;
  }

  app.innerHTML = `
    ${renderTopNav()}
    <div class="main-container">
      ${renderApprovalBanners()}
      ${renderSelectorBar()}
      ${renderPermissionBar()}
      ${renderTabs()}
      ${state.activeTab === 'board' ? renderBoard() : ''}
      ${state.activeTab === 'pipeline' ? renderPipeline() : ''}
      ${state.activeTab === 'governance' ? renderGovernance() : ''}
    </div>
    ${renderTriggerDialog()}
    <div id="toast" class="toast"></div>
  `;
}

function renderLogin() {
  return `
    <div class="top-nav">
      <div class="top-nav-left">
        <a class="top-nav-brand" href="#">
          <div class="top-nav-brand-icon">D</div>
          DevAI
        </a>
      </div>
    </div>
    <div class="login-page">
      <div class="login-card">
        <h2 class="h3">Welcome to DevAI</h2>
        <p class="text-body-sm text-muted">AI-powered development lifecycle automation.<br>Connect your GitHub organization to get started.</p>
        <button class="btn btn-primary btn-lg" onclick="login()">
          <svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor"><path d="M12 0C5.37 0 0 5.37 0 12c0 5.31 3.435 9.795 8.205 11.385.6.105.825-.255.825-.57 0-.285-.015-1.23-.015-2.235-3.015.555-3.795-.735-4.035-1.41-.135-.345-.72-1.41-1.23-1.695-.42-.225-1.02-.78-.015-.795.945-.015 1.62.87 1.845 1.23 1.08 1.815 2.805 1.305 3.495.99.105-.78.42-1.305.765-1.605-2.67-.3-5.46-1.335-5.46-5.925 0-1.305.465-2.385 1.23-3.225-.12-.3-.54-1.53.12-3.18 0 0 1.005-.315 3.3 1.23.96-.27 1.98-.405 3-.405s2.04.135 3 .405c2.295-1.56 3.3-1.23 3.3-1.23.66 1.65.24 2.88.12 3.18.765.84 1.23 1.905 1.23 3.225 0 4.605-2.805 5.625-5.475 5.925.435.375.81 1.095.81 2.22 0 1.605-.015 2.895-.015 3.3 0 .315.225.69.825.57A12.02 12.02 0 0024 12c0-6.63-5.37-12-12-12z"/></svg>
          Connect with GitHub
        </button>
      </div>
    </div>
  `;
}

function renderTopNav() {
  return `
    <nav class="top-nav">
      <div class="top-nav-left">
        <a class="top-nav-brand" href="#">
          <div class="top-nav-brand-icon">D</div>
          DevAI
        </a>
        <div class="top-nav-links">
          <button class="top-nav-link ${state.activeTab === 'board' ? 'active' : ''}" onclick="switchTab('board')">Board</button>
          <button class="top-nav-link ${state.activeTab === 'pipeline' ? 'active' : ''}" onclick="switchTab('pipeline')">Pipeline</button>
          <button class="top-nav-link ${state.activeTab === 'governance' ? 'active' : ''}" onclick="switchTab('governance')">Governance</button>
        </div>
      </div>
      <div class="top-nav-actions">
        <button class="btn btn-primary btn-sm" onclick="openDialog('trigger-dialog')">New Pipeline</button>
        <div class="user-menu">
          <img class="avatar" src="${state.user.avatar_url}" alt="${state.user.login}">
          <span>${state.user.login}</span>
          <button class="btn btn-ghost btn-icon-sm" onclick="logout()" title="Logout">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 21H5a2 2 0 01-2-2V5a2 2 0 012-2h4M16 17l5-5-5-5M21 12H9"/></svg>
          </button>
        </div>
      </div>
    </nav>
  `;
}

function renderApprovalBanners() {
  if (!state.pendingApprovals.length) return '';
  return state.pendingApprovals.map(a => `
    <div class="approval-banner">
      <div class="approval-banner-text">
        <div class="approval-banner-title">Approval Required: ${a.gate}</div>
        <div class="approval-banner-desc">Run ${a.runId.slice(0, 12)}... is waiting for your approval to proceed with <strong>${a.gate}</strong>.</div>
      </div>
      <div class="approval-banner-actions">
        <button class="btn btn-success btn-sm" onclick="approveGate('${a.runId}', '${a.gate}')">Approve</button>
        <button class="btn btn-destructive btn-sm" onclick="rejectGate('${a.runId}', '${a.gate}')">Reject</button>
      </div>
    </div>
  `).join('');
}

function renderSelectorBar() {
  return `
    <div class="selector-bar">
      <select class="form-select" onchange="selectOrg(this.value)" style="min-width: 180px">
        <option value="">Select Organization</option>
        ${state.orgs.map(o => `<option value="${o.login}" ${o.login === state.selectedOrg ? 'selected' : ''}>${o.login}</option>`).join('')}
      </select>
      ${state.selectedOrg ? `
        <select class="form-select" onchange="state.selectedRepo = this.value; loadClaudeMd(); render();" style="min-width: 220px">
          <option value="">Select Repository</option>
          ${state.repos.map(r => `<option value="${r.full_name}" ${r.full_name === state.selectedRepo ? 'selected' : ''}>${r.name} ${r.language ? '(' + r.language + ')' : ''}</option>`).join('')}
        </select>
        <select class="form-select" onchange="selectProject(Number(this.value))" style="min-width: 220px">
          <option value="">Select Project</option>
          ${state.projects.map(p => `<option value="${p.number}" ${state.selectedProject?.number === p.number ? 'selected' : ''}>${p.title}</option>`).join('')}
        </select>
        <button class="btn btn-outline btn-sm" onclick="createProject()">+ New Project</button>
      ` : ''}
    </div>
  `;
}

function renderPermissionBar() {
  const gates = state.permissions.gates;
  return `
    <div class="permission-bar">
      <div class="permission-bar-label">Approval Mode</div>
      <label class="switch">
        <input type="checkbox" ${state.permissions.autoMode ? 'checked' : ''} onchange="toggleAutoMode()">
        <span class="switch-track"></span>
      </label>
      <div class="permission-bar-desc">
        ${state.permissions.autoMode
          ? '<strong style="color: var(--success)">Auto</strong> — Pipeline runs without stopping. All gates bypassed.'
          : '<strong style="color: var(--warning)">Manual</strong> — Pipeline pauses at selected gates for your approval.'}
      </div>
      ${!state.permissions.autoMode ? `
        <div class="permission-gates">
          ${Object.entries(gates).map(([gate, active]) => `
            <button class="permission-gate ${active ? 'active' : ''}" onclick="toggleGate('${gate}')">
              ${active ? '&#10003;' : ''} ${gate}
            </button>
          `).join('')}
        </div>
      ` : ''}
    </div>
  `;
}

function renderTabs() {
  return `
    <div class="tabs">
      <button class="tab ${state.activeTab === 'board' ? 'active' : ''}" onclick="switchTab('board')">Board</button>
      <button class="tab ${state.activeTab === 'pipeline' ? 'active' : ''}" onclick="switchTab('pipeline')">Pipeline Runs</button>
      <button class="tab ${state.activeTab === 'governance' ? 'active' : ''}" onclick="switchTab('governance')">Governance (CLAUDE.md)</button>
    </div>
  `;
}

function renderBoard() {
  if (!state.selectedProject) {
    return `
      <div class="empty-state">
        <div class="empty-state-icon">&#9776;</div>
        <h3 class="h5">No project selected</h3>
        <p class="text-body-sm text-muted">Select an organization and project above, or create a new GitHub Project to get started.</p>
      </div>
    `;
  }

  const cols = state.board.columns || {};
  const colDefs = [
    { key: 'Backlog', class: 'col-backlog', label: 'Backlog' },
    { key: 'Ready', class: 'col-ready', label: 'Ready' },
    { key: 'In Progress', class: 'col-inprogress', label: 'In Progress' },
    { key: 'Done', class: 'col-done', label: 'Done' },
  ];

  return `
    <div class="board">
      ${colDefs.map(col => {
        const items = cols[col.key] || [];
        return `
          <div class="column ${col.class}">
            <div class="column-header">
              <div class="column-header-left">
                <span class="column-dot"></span>
                ${col.label}
              </div>
              <span class="badge badge-neutral column-count">${items.length}</span>
            </div>
            <div class="column-body">
              ${items.length === 0 ? '<div class="empty-state" style="padding: 24px"><p class="text-caption text-muted">No items</p></div>' : ''}
              ${items.map(item => renderCard(item, col.key)).join('')}
            </div>
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function renderCard(item, currentCol) {
  const moveOptions = ['Backlog', 'Ready', 'In Progress', 'Done'].filter(c => c !== currentCol);
  const labelClass = (l) => {
    if (l.includes('devai')) return 'card-label-devai';
    if (l.includes('feature')) return 'card-label-feature';
    if (l.includes('bug')) return 'card-label-bug';
    if (l.includes('priority')) return 'card-label-priority';
    return 'card-label-devai';
  };

  return `
    <div class="card" onclick="window.open('${item.url}', '_blank')">
      <div class="card-title">${escapeHtml(item.title)}</div>
      <div class="card-meta">
        <span class="card-number">#${item.issue_number}</span>
        ${item.assignees?.length ? `<span>${item.assignees[0]}</span>` : ''}
      </div>
      ${item.labels?.length ? `
        <div class="card-labels">
          ${item.labels.map(l => `<span class="card-label ${labelClass(l)}">${escapeHtml(l)}</span>`).join('')}
        </div>
      ` : ''}
      <div class="card-actions" onclick="event.stopPropagation()">
        ${moveOptions.map(opt => `
          <button class="btn btn-ghost btn-sm" style="font-size: 11px; height: 24px; padding: 0 6px;" onclick="event.stopPropagation(); moveItem('${item.item_id}', '${opt}')">
            &rarr; ${opt}
          </button>
        `).join('')}
        ${currentCol !== 'Done' && item.labels?.some(l => l.includes('user-story')) ? `
          <button class="btn btn-primary btn-sm" style="font-size: 11px; height: 24px; padding: 0 8px;" onclick="event.stopPropagation(); triggerPipeline(${item.issue_number})">
            Run DevAI
          </button>
        ` : ''}
      </div>
    </div>
  `;
}

function renderPipeline() {
  if (!state.runs.length) {
    return `
      <div class="empty-state">
        <div class="empty-state-icon">&#9881;</div>
        <h3 class="h5">No pipeline runs yet</h3>
        <p class="text-body-sm text-muted">Trigger a pipeline from the board or use the "New Pipeline" button above.</p>
      </div>
    `;
  }

  const agentOrder = ['product_director', 'engineering_manager', 'senior_developer', 'staff_reviewer', 'qa_tester'];
  const agentLabels = ['PD', 'EM', 'Sr Dev', 'Staff', 'QA'];

  return `
    <div class="pipeline-runs">
      ${state.runs.map(run => {
        const agents = run.agents || {};
        return `
          <div class="run-card">
            <div class="run-header">
              <div>
                <span class="run-id">${run.run_id?.slice(0, 16)}...</span>
                <span class="run-repo">${run.repo || ''}</span>
              </div>
              <span class="badge stage-${run.stage}">${run.stage}</span>
            </div>
            <div class="agent-pipeline">
              ${agentOrder.map(a => {
                const s = agents[a]?.status || 'pending';
                return `<div class="agent-step ${s}" title="${a}: ${s}"></div>`;
              }).join('')}
            </div>
            <div class="agent-labels">
              ${agentLabels.map(l => `<span class="agent-label">${l}</span>`).join('')}
            </div>
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function renderGovernance() {
  return `
    <div style="display: grid; gap: 24px;">
      <div class="card" style="padding: 24px; cursor: default;">
        <div style="display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px;">
          <div>
            <h3 class="h5">CLAUDE.md Governance</h3>
            <p class="text-body-sm text-muted" style="margin-top: 4px;">
              Define instructions, conventions, and rules that all AI agents must follow when working on this repository.
              This content is injected into every agent's system prompt.
            </p>
          </div>
          <button class="btn btn-primary btn-sm" onclick="saveClaudeMd()">Save CLAUDE.md</button>
        </div>
        <textarea
          id="claude-md-editor"
          class="form-textarea"
          style="min-height: 400px; font-family: var(--font-mono); font-size: 13px; line-height: 1.6;"
          placeholder="# Project Conventions&#10;&#10;## Git Identity&#10;Always use: git config user.name &quot;sam123ben&quot;&#10;&#10;## Code Style&#10;- Follow existing patterns&#10;- No AI references in commits or code&#10;&#10;## Architecture Rules&#10;- Use Gin for HTTP handlers&#10;- PostgreSQL for persistence&#10;- All services behind Istio&#10;&#10;## Testing&#10;- Write unit tests for all business logic&#10;- E2E tests with Playwright&#10;&#10;## Security&#10;- No secrets in code&#10;- Use GCP Secret Manager&#10;- OWASP Top 10 compliance"
          oninput="updateClaudeMd()"
        >${escapeHtml(state.claudeMd)}</textarea>
        <p class="form-helper" style="margin-top: 8px;">
          ${state.selectedRepo
            ? `Will be applied to: <strong>${state.selectedRepo}</strong>`
            : 'Select a repository to associate this CLAUDE.md with.'}
        </p>
      </div>

      <div class="card" style="padding: 24px; cursor: default;">
        <h3 class="h5" style="margin-bottom: 12px;">Agent Behavior Settings</h3>
        <div style="display: grid; grid-template-columns: repeat(2, 1fr); gap: 16px;">
          <div class="form-group">
            <label class="form-label">Max Review Iterations</label>
            <input type="number" class="form-input" value="3" min="1" max="10" style="width: 100px;">
            <p class="form-helper">How many times the Staff Reviewer can request changes before failing.</p>
          </div>
          <div class="form-group">
            <label class="form-label">Claude Model</label>
            <select class="form-select">
              <option value="claude-sonnet-5">Claude Sonnet 5</option>
              <option value="claude-opus-4-6">Claude Opus 4.6</option>
            </select>
            <p class="form-helper">Model used for EM, Sr Dev, and QA agents.</p>
          </div>
          <div class="form-group">
            <label class="form-label">OpenAI Model</label>
            <select class="form-select">
              <option value="o3">O3</option>
              <option value="gpt-4.1">GPT-4.1</option>
            </select>
            <p class="form-helper">Model used for Product Director and Staff Reviewer.</p>
          </div>
          <div class="form-group">
            <label class="form-label">Branch Naming</label>
            <input type="text" class="form-input" value="devai/{run_id}/{description}" placeholder="devai/{run_id}/{description}">
            <p class="form-helper">Template for feature branch names.</p>
          </div>
        </div>
      </div>
    </div>
  `;
}

function renderTriggerDialog() {
  return `
    <div id="trigger-dialog" class="dialog-overlay" onclick="if(event.target===this)closeDialog()">
      <div class="dialog">
        <div class="dialog-header">
          <h3 class="dialog-title">New Pipeline Run</h3>
          <p class="dialog-description">Provide requirements to kick off the full development lifecycle.</p>
        </div>
        <div class="form-group">
          <label class="form-label">Repository</label>
          <select class="form-select" id="trigger-repo" onchange="state.selectedRepo = this.value">
            <option value="">Select repository</option>
            ${state.repos.map(r => `<option value="${r.full_name}" ${r.full_name === state.selectedRepo ? 'selected' : ''}>${r.full_name}</option>`).join('')}
          </select>
        </div>
        <div class="form-group">
          <label class="form-label">Requirements</label>
          <textarea id="requirements-input" class="form-textarea" placeholder="Describe what you want to build. The Product Director AI will break this into user stories, the Engineering Manager will plan, the Sr Developer will code, the Staff Reviewer will review, and QA will test."></textarea>
        </div>
        <div class="dialog-footer">
          <button class="btn btn-outline" onclick="closeDialog()">Cancel</button>
          <button class="btn btn-primary" onclick="triggerFromRequirements()">Launch Pipeline</button>
        </div>
      </div>
    </div>
  `;
}

// --- Helpers ---
function escapeHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}
