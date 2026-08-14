"use client";

import { useEffect, useMemo, useState } from "react";
import { Cpu, Plug } from "lucide-react";
import { Select, type SelectOption } from "@/components/ui/select";

/**
 * Provider and model, chosen together.
 *
 * A model id alone is not a choice: the same Claude model is one thing on an
 * Anthropic key and another through the gateway or Vertex — different
 * credentials, quota and billing. So the provider picks first and the model
 * list follows it, from `/api/models` (the same catalog the router uses, so the
 * UI cannot offer a pairing the backend would then override).
 */

export type ModelInfo = { id: string; label: string; tier: string };
export type ProviderInfo = {
  id: string;
  label: string;
  description?: string;
  default_model: string;
  connected?: boolean;
  models: ModelInfo[];
};

const CUSTOM = "__custom__";

// The catalog changes when the platform is redeployed, not while a page is
// open, so one fetch per tab is enough for every picker on it.
let cached: Promise<{ providers: ProviderInfo[]; default_provider: string }> | null = null;

export function loadModelCatalog() {
  cached ??= fetch("/api/models", { credentials: "include" })
    .then((r) => (r.ok ? r.json() : { providers: [], default_provider: "anthropic" }))
    .catch(() => ({ providers: [], default_provider: "anthropic" }));
  return cached;
}

export function useModelCatalog() {
  const [providers, setProviders] = useState<ProviderInfo[]>([]);
  useEffect(() => {
    loadModelCatalog().then((c) => setProviders(c.providers ?? []));
  }, []);
  return providers;
}

export function ModelPicker({
  provider,
  model,
  onChange,
  className = "",
}: {
  provider: string;
  model: string;
  onChange: (next: { provider: string; model: string }) => void;
  className?: string;
}) {
  const providers = useModelCatalog();
  const [custom, setCustom] = useState(false);

  const current = providers.find((p) => p.id === provider);

  const providerOptions: SelectOption[] = useMemo(
    () =>
      providers.map((p) => ({
        value: p.id,
        label: p.label,
        description: p.description,
        badge: p.connected ? "connected" : undefined,
      })),
    [providers],
  );

  const modelOptions: SelectOption[] = useMemo(() => {
    const own = (current?.models ?? []).map((m) => ({
      value: m.id,
      label: m.label,
      description: m.id,
      badge: m.tier,
      group: current?.label,
    }));
    // The same model served by another provider stays reachable — picking it
    // switches the provider with it, rather than leaving an unservable pin.
    const elsewhere = providers
      .filter((p) => p.id !== provider)
      .flatMap((p) =>
        p.models.map((m) => ({
          value: `${p.id}:${m.id}`,
          label: m.label,
          description: m.id,
          badge: p.connected ? "connected" : undefined,
          group: p.label,
        })),
      );
    return [...own, ...elsewhere, { value: CUSTOM, label: "Other model id…", group: "Custom" }];
  }, [current, providers, provider]);

  function pickModel(value: string) {
    if (value === CUSTOM) {
      setCustom(true);
      return;
    }
    setCustom(false);
    const [maybeProvider, ...rest] = value.split(":");
    if (rest.length > 0 && providers.some((p) => p.id === maybeProvider)) {
      onChange({ provider: maybeProvider, model: rest.join(":") });
      return;
    }
    onChange({ provider, model: value });
  }

  const known = (current?.models ?? []).some((m) => m.id === model);

  return (
    <div className={`grid grid-cols-2 gap-3 ${className}`}>
      <div>
        <label className="label-eyebrow" htmlFor="mp-provider">
          Provider
        </label>
        <Select
          id="mp-provider"
          value={provider}
          onChange={(id) => {
            const next = providers.find((p) => p.id === id);
            onChange({ provider: id, model: next?.default_model ?? "" });
          }}
          options={providerOptions}
          placeholder={providers.length === 0 ? "Loading…" : "Choose a provider"}
          emptyLabel="No providers — /api/models is unreachable."
          icon={<Plug className="w-3.5 h-3.5 shrink-0" style={{ color: "var(--ink-muted)" }} />}
          ariaLabel="Model provider"
        />
      </div>
      <div>
        <label className="label-eyebrow" htmlFor="mp-model">
          Model
        </label>
        {custom || (providers.length > 0 && !known && model) ? (
          <div className="flex gap-2">
            <input
              value={model}
              onChange={(e) => onChange({ provider, model: e.target.value })}
              placeholder="model id"
              className="field w-full font-mono !text-[13px]"
              aria-label="Model id"
            />
            <button
              type="button"
              onClick={() => {
                setCustom(false);
                onChange({ provider, model: current?.default_model ?? "" });
              }}
              className="btn-secondary !py-1 !px-2 !text-[11px] shrink-0"
            >
              List
            </button>
          </div>
        ) : (
          <Select
            id="mp-model"
            value={model}
            onChange={pickModel}
            options={modelOptions}
            mono
            placeholder={current ? "Choose a model" : "Pick a provider first"}
            icon={<Cpu className="w-3.5 h-3.5 shrink-0" style={{ color: "var(--ink-muted)" }} />}
            ariaLabel="Model"
          />
        )}
      </div>
    </div>
  );
}
