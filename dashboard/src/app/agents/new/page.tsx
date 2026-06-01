"use client";

import { useRouter } from "next/navigation";

import ArtifactEditor from "@/components/artifact-editor";
import { useToast } from "@/components/toast";

/**
 * Create Agent — the same form + live YAML/JSON manifest + validation as the
 * agent registry. The Agent spec composes registry Skills, Tools, MCP servers
 * and Prompts via reference pickers; publishing posts the registry CR manifest
 * so the agent reflects in both DevAI and the aregistry marketplace.
 */
export default function NewAgentPage() {
  const router = useRouter();
  const toast = useToast();
  return (
    <ArtifactEditor
      kind="Agent"
      open
      onClose={() => router.push("/agents")}
      onCreated={(name) => {
        toast.success(`Published agent "${name}" to the registry.`);
        router.push("/agents");
      }}
    />
  );
}
