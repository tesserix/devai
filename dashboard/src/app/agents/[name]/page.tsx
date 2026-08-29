import { AgentWorkbench } from "@/components/agent-workbench";

export default async function AgentPage({ params }: { params: Promise<{ name: string }> }) {
  const { name } = await params;
  return <AgentWorkbench agentName={name} />;
}
