(() => {
  const main = document.getElementById("main");
  let view = "policies";
  let lastReconcile = null;

  async function api(path, opts) {
    const res = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...opts,
    });
    if (res.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      throw new Error(data.detail || data.error || "请求失败");
    }
    return data;
  }

  function tag(state) {
    const cls =
      state === "在线" || state === "待执行"
        ? "warn"
        : state === "已完成" || state === "已归档" || state === "已销毁"
          ? "ok"
          : state === "已失败"
            ? "bad"
            : "";
    return `<span class="tag ${cls}">${state}</span>`;
  }

  function archiveAddressCell(p) {
    if (p.storage_state !== "已归档") {
      return p.archive_uri ? `<span class="mono">${p.archive_uri}</span>` : "—";
    }
    if (p.archive_uri_missing) {
      return `<span class="tag bad">缺址拦截</span><span class="sub">已归档但无归档地址，对账将报错位</span>`;
    }
    const digest = p.snapshot ? p.snapshot.content_digest : "";
    return `<span class="mono">${p.archive_uri}</span>` +
      (digest ? `<br><span class="sub">快照 ${p.snapshot.row_count} 行 · 摘要 ${digest}</span>` : "");
  }

  async function renderPolicies() {
    const data = await api("/api/policies");
    const overview = Object.fromEntries(
      (data.archive_overview || []).map((o) => [o.domain_code, o])
    );
    main.innerHTML = `
      <section class="panel">
        <h2>现行留存策略</h2>
        <p class="sub">每个主题域一条现行版本：保留天数与到期动作（归档或销毁）。右侧核对该域归档地址齐全性。</p>
        <table>
          <thead><tr><th>主题域</th><th>编码</th><th>保留天</th><th>到期动作</th><th>版本</th><th>归档核对（已归档/缺址/快照）</th></tr></thead>
          <tbody>
            ${data.items
              .map((p) => {
                const o = overview[p.domain_code] || { archived_count: 0, missing_uri_count: 0, snapshot_count: 0 };
                const check = o.missing_uri_count > 0
                  ? `<span class="tag bad">缺址 ${o.missing_uri_count}</span>`
                  : `<span class="tag ok">地址齐全</span>`;
                return `<tr>
              <td>${p.domain_name}</td>
              <td class="mono">${p.domain_code}</td>
              <td>${p.keep_days}</td>
              <td>${tag(p.action_on_expire)}</td>
              <td>v${p.version}</td>
              <td>${check} <span class="mono sub">${o.archived_count}/${o.missing_uri_count}/${o.snapshot_count}</span></td>
            </tr>`;
              })
              .join("")}
          </tbody>
        </table>
      </section>`;
  }

  async function renderPartitions() {
    const data = await api("/api/partitions");
    main.innerHTML = `
      <section class="panel">
        <h2>实物分区</h2>
        <p class="sub">在线 / 已归档 / 已销毁 / 裁撤中。数字与后续对账同源。</p>
        <table>
          <thead><tr><th>主题域</th><th>业务日</th><th>行数</th><th>存储态</th><th>归档地址</th></tr></thead>
          <tbody>
            ${data.items
              .map(
                (p) => `<tr>
              <td class="mono">${p.domain_code}</td>
              <td class="mono">${p.biz_date}</td>
              <td>${p.row_count}</td>
              <td>${tag(p.storage_state)}${p.archive_uri_missing ? ' <span class="tag bad">无址归档</span>' : ""}</td>
              <td>${archiveAddressCell(p)}</td>
            </tr>`
              )
              .join("")}
          </tbody>
        </table>
      </section>`;
  }

  async function renderScan() {
    const jobs = await api("/api/scan-jobs");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of" value="2026-11-16" />
        </label>
        <button type="button" id="btn-scan">跑到期扫描</button>
      </div>
      <section class="panel">
        <h2>扫描任务</h2>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>状态</th><th>到期数</th><th>说明</th></tr></thead>
          <tbody>
            ${
              jobs.items.length
                ? jobs.items
                    .map(
                      (j) => `<tr>
              <td>${j.id}</td>
              <td class="mono">${j.as_of_date}</td>
              <td>${tag(j.status)}</td>
              <td>${j.expired_count}</td>
              <td>${j.message}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="5" class="empty">尚无扫描任务</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    document.getElementById("btn-scan").onclick = async () => {
      const as_of_date = document.getElementById("as-of").value.trim();
      await api("/api/scan-jobs", {
        method: "POST",
        body: JSON.stringify({ as_of_date }),
      });
      await renderScan();
    };
  }

  async function renderJudgments() {
    const [data, runsData] = await Promise.all([
      api("/api/judgments"),
      api("/api/runs"),
    ]);
    const runByJudgment = Object.fromEntries(
      runsData.items.map((r) => [r.judgment_id, r])
    );
    main.innerHTML = `
      <section class="panel">
        <h2>裁决清单</h2>
        <p class="sub">待执行可点「执行」。归档执行同事务写入归档地址与实物快照；缺址即拦，失败原因回显。</p>
        <table>
          <thead><tr><th>编号</th><th>主题域</th><th>业务日</th><th>策略</th><th>动作</th><th>状态</th><th>执行进度 / 归档地址与快照</th><th>操作</th></tr></thead>
          <tbody>
            ${
              data.items.length
                ? data.items
                    .map((j) => {
                      const run = runByJudgment[j.id];
                      let progress = "—";
                      if (run) {
                        if (run.ok === 1) {
                          if (run.action === "归档") {
                            progress = `${tag(run.result_state)}<br><span class="mono sub">${run.archive_uri || "（缺址）"}</span><br><span class="sub">${run.detail}</span>`;
                          } else {
                            progress = `${tag(run.result_state)}<br><span class="sub">${run.detail}</span>`;
                          }
                        } else {
                          progress = `<span class="tag bad">已失败</span><br><span class="sub">${run.detail || j.fail_reason || ""}</span>`;
                        }
                      } else if (j.fail_reason) {
                        progress = `<span class="sub">${j.fail_reason}</span>`;
                      }
                      return `<tr>
              <td>${j.id}</td>
              <td class="mono">${j.domain_code}</td>
              <td class="mono">${j.biz_date}</td>
              <td>v${j.policy_version} / ${j.keep_days}天</td>
              <td>${j.decided_action}</td>
              <td>${tag(j.status)}${j.fail_reason ? `<br><span class="sub">${j.fail_reason}</span>` : ""}</td>
              <td>${progress}</td>
              <td>${
                j.status === "待执行" || j.status === "已失败"
                  ? `<button type="button" data-exec="${j.id}">执行</button>`
                  : "—"
              }</td>
            </tr>`;
                    })
                    .join("")
                : `<tr><td colspan="8" class="empty">尚无裁决，请先跑到期扫描</td></tr>`
            }
          </tbody>
        </table>
      </section>`;
    main.querySelectorAll("[data-exec]").forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/api/judgments/${btn.dataset.exec}/execute`, { method: "POST" });
        } catch (err) {
          window.alert(err.message);
        }
        await renderJudgments();
      };
    });
  }

  function renderMismatchBox() {
    const mm = (lastReconcile && lastReconcile.mismatches) || [];
    if (!lastReconcile) {
      return `<p class="sub">生成对账报告后，此处展示错位明细（含缺址归档）。</p>`;
    }
    if (!mm.length) {
      return `<p class="sub">报告 #${lastReconcile.id} 无错位：归档地址与实物快照齐全。</p>`;
    }
    return `<table><thead><tr><th>裁决</th><th>分区</th><th>动作</th><th>存储态</th><th>错位类型</th><th>归档地址</th></tr></thead><tbody>${mm
      .map(
        (m) => `<tr><td>${m.judgment_id}</td><td>${m.partition_id}</td><td>${m.decided_action}</td><td>${m.storage_state}</td><td><span class="tag bad">${m.mismatch_type}</span></td><td class="mono">${m.archive_uri || "（空）"}</td></tr>`
      )
      .join("")}</tbody></table>`;
  }

  async function renderReconcile() {
    const reports = await api("/api/reconcile-reports");
    main.innerHTML = `
      <div class="toolbar">
        <label class="field">基准日
          <input id="as-of-r" value="2026-11-16" />
        </label>
        <button type="button" id="btn-rec">生成对账报告</button>
      </div>
      <section class="panel">
        <h2>对账报告</h2>
        <table>
          <thead><tr><th>编号</th><th>基准日</th><th>在线</th><th>归档</th><th>销毁</th><th>待裁决</th><th>错位</th><th>摘要</th></tr></thead>
          <tbody>
            ${
              reports.items.length
                ? reports.items
                    .map(
                      (r) => `<tr>
              <td>${r.id}</td>
              <td class="mono">${r.as_of_date}</td>
              <td>${r.online_count}</td>
              <td>${r.archived_count}</td>
              <td>${r.destroyed_count}</td>
              <td>${r.pending_judgment_count}</td>
              <td>${r.mismatch_count > 0 ? `<span class="tag bad">${r.mismatch_count}</span>` : r.mismatch_count}</td>
              <td>${r.summary}</td>
            </tr>`
                    )
                    .join("")
                : `<tr><td colspan="8" class="empty">尚无对账报告</td></tr>`
            }
          </tbody>
        </table>
        <p class="sub">错位口径：归档裁决已完成但存储态不符，或已归档却缺归档地址（missing_archive_uri）。最新报告明细如下。</p>
        <div id="rec-mismatches">${renderMismatchBox()}</div>
      </section>`;
    document.getElementById("btn-rec").onclick = async () => {
      const as_of_date = document.getElementById("as-of-r").value.trim();
      const resp = await api("/api/reconcile-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date }),
      });
      lastReconcile = resp.report || null;
      await renderReconcile();
    };
  }

  const renderers = {
    policies: renderPolicies,
    partitions: renderPartitions,
    scan: renderScan,
    judgments: renderJudgments,
    reconcile: renderReconcile,
  };

  async function show(name) {
    view = name;
    document.querySelectorAll("#nav button").forEach((b) => {
      b.classList.toggle("active", b.dataset.view === name);
    });
    await renderers[name]();
  }

  document.getElementById("nav").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-view]");
    if (btn) show(btn.dataset.view);
  });

  document.getElementById("btn-logout").onclick = async () => {
    await api("/api/logout", { method: "POST" });
    location.href = "/login";
  };

  api("/api/meta").then((m) => {
    document.getElementById("lab-title").textContent = m.lab_name;
    document.getElementById("lab-sub").textContent = m.skeleton;
  });

  show(view);
})();
