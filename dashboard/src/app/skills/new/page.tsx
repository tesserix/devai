"use client";

import { useRouter } from "next/navigation";

import ArtifactEditor from "@/components/artifact-editor";
import { useToast } from "@/components/toast";

/**
 * Author a Skill — the same form + live YAML/JSON manifest + validation as the
 * agent registry. Publishing posts the registry CR manifest through DevAI's
 * write path, so the skill shows up in both DevAI and the aregistry marketplace.
 */
export default function NewSkillPage() {
  const router = useRouter();
  const toast = useToast();
  return (
    <ArtifactEditor
      kind="Skill"
      open
      onClose={() => router.push("/skills")}
      onCreated={(name) => {
        toast.success(`Published skill "${name}" to the registry.`);
        router.push("/skills");
      }}
    />
  );
}
