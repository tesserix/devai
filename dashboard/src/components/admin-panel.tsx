"use client";

/**
 * Admin-only platform view — who uses DevAI.
 *
 * Visibility is decided by the API, not the client: /api/admin/overview
 * answers 403 for non-admins and the caller renders nothing. No email or
 * role is checked here, so the tab cannot be revealed by editing state.
 *
 * Two sources, different exactness, labelled as such:
 * - active users / sign-ins / per-user usage — exact, server-side
 * - visitors / sessions — approximate, client-reported via OpenPanel
 */

import { useEffect, useState } from "react";
import { Users, MousePointerClick, LogIn } from "lucide-react";
import { api, type AdminOpenPanel, type AdminOverview } from "@/lib/api";
import { HBarChart, LineChart } from "@/components/charts";

export function AdminPanel({ days }: { days: number }) {
  const [overview, setOverview] = useState<AdminOverview | null>(null);
  const [panel, setPanel] = useState<AdminOpenPanel | null>(null);

  useEffect(() => {
    let live = true;
    api.admin
      .overview(days)
      .then((d) => live && setOverview(d))
      .catch(() => live && setOverview(null));
    api.admin
      .openpanel(days)
      .then((d) => live && setPanel(d))
      .catch(() => live && setPanel({ enabled: false, reason: "unavailable" }));
    return () => {
      live = false;
    };
  }, [days]);

  if (!overview) return null;

  const peak = overview.active_users.reduce((m, p) => Math.max(m, p.users), 0);

  return (
    <div className="space-y-6">
      <section className="grid grid-cols-2 md:grid-cols-3 gap-3">
        <Stat icon={<Users className="w-4 h-4" />} label="Peak daily active users" value={String(peak)} note="Exact" />
        <Stat icon={<LogIn className="w-4 h-4" />} label="Sign-ins" value={String(overview.signins)} note="Local sign-ins only" />
        <Stat
          icon={<MousePointerClick className="w-4 h-4" />}
          label="Visitors"
          value={panel?.enabled ? String(panel.visitors ?? 0) : "—"}
          note={panel?.enabled ? "Client-reported, approximate" : "OpenPanel not configured"}
        />
      </section>

      <Section title="Active users">
        {overview.active_users.length > 0 ? (
          <LineChart
            series={[
              {
                name: "Active users",
                color: "var(--accent)",
                points: overview.active_users.map((p) => ({ label: p.date, value: p.users })),
              },
            ]}
          />
        ) : (
          <Empty>No activity recorded yet.</Empty>
        )}
      </Section>

      <Section title="LLM cost by user">
        {overview.by_user.length > 0 ? (
          <HBarChart
            rows={overview.by_user.map((u) => ({ label: u.user, value: u.cost_usd }))}
            formatValue={(n) => `$${n.toFixed(2)}`}
          />
        ) : (
          <Empty>No metered usage yet.</Empty>
        )}
      </Section>

      <Section title="Days active by user">
        {overview.user_activity.length > 0 ? (
          <HBarChart rows={overview.user_activity.map((u) => ({ label: u.user, value: u.days_active }))} />
        ) : (
          <Empty>No activity recorded yet.</Empty>
        )}
      </Section>
    </div>
  );
}

function Stat({ icon, label, value, note }: { icon: React.ReactNode; label: string; value: string; note: string }) {
  return (
    <div className="panel" style={{ padding: 14 }}>
      <div className="flex items-center gap-2" style={{ color: "var(--ink-muted)" }}>
        {icon}
        <span className="label-eyebrow">{label}</span>
      </div>
      <div className="font-mono tabular-nums" style={{ fontSize: 26, fontWeight: 600, color: "var(--ink-strong)", marginTop: 4 }}>
        {value}
      </div>
      <div style={{ fontSize: 11, color: "var(--ink-muted)", marginTop: 2 }}>{note}</div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="panel" style={{ padding: 16 }}>
      <h3 className="font-mono" style={{ fontSize: 13, fontWeight: 600, color: "var(--ink-strong)", marginBottom: 12 }}>
        {title}
      </h3>
      {children}
    </section>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p style={{ fontSize: 13, color: "var(--ink-muted)" }}>{children}</p>;
}
