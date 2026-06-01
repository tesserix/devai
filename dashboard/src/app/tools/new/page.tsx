"use client";

import { useRouter } from "next/navigation";

import ArtifactEditor from "@/components/artifact-editor";
import { useToast } from "@/components/toast";

/**
 * Register a Tool / MCP server — the same form + live YAML/JSON manifest +
 * validation as the agent registry. Publishing posts the registry CR manifest
 * so the server reflects in both DevAI and the aregistry marketplace and can be
 * picked from the Create-Agent MCP servers list.
 */
export default function NewToolPage() {
  const router = useRouter();
  const toast = useToast();
  return (
    <ArtifactEditor
      kind="MCPServer"
      open
      onClose={() => router.push("/tools")}
      onCreated={(name) => {
        toast.success(`Published MCP server "${name}" to the registry.`);
        router.push("/tools");
      }}
    />
  );
}
