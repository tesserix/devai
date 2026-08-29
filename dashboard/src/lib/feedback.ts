export type FeedbackStatus = "open" | "closed";

export function feedbackStatusLabel(status: FeedbackStatus): string {
  return status === "closed" ? "Resolved" : "Open";
}

export function feedbackInboxTitle(canManage: boolean): string {
  return canManage ? "Support inbox" : "Your feedback";
}
