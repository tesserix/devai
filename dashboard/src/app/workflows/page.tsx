import { Construction } from "lucide-react";

export default function Page() {
  const label = "workflows";
  return (
    <div className="p-7">
      <div className="label-eyebrow">Mission control</div>
      <h1 className="font-serif text-2xl font-medium text-[var(--ink-50)] mt-1 capitalize">{label}</h1>
      <div className="panel mt-6 p-6 text-sm text-[var(--ink-300)] flex items-start gap-3">
        <Construction className="w-5 h-5 text-indigo-400 mt-0.5 shrink-0" />
        <div>
          <p className="text-[var(--ink-100)]">Coming soon.</p>
          <p className="mt-1">
            This panel is reserved in the mission-control shell so the per-feature work can ship without further restructure.
          </p>
        </div>
      </div>
    </div>
  );
}
