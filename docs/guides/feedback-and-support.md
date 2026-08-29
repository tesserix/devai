# Feedback and support

The DevAI feedback inbox keeps a request and its support conversation together
until it is resolved. Use it for a user story or requirement, a bug report, or an
improvement task.

## Create a request

1. Sign in at [devai.tesserix.app](https://devai.tesserix.app).
2. Open **Feedback** in the navigation, or go directly to
   [devai.tesserix.app/feedback](https://devai.tesserix.app/feedback).
3. Select **User story / requirement**, **Bug report**, or
   **Task / other improvement**.
4. Enter a clear title and enough detail for support to reproduce or understand
   the request.
5. Select **Submit feedback**.

The new thread appears in **Your feedback** with its issue number and current
status. Opening the thread shows the original request, replies, timestamps, and a
link to the backing GitHub issue. GitHub access depends on repository permissions
and is not required to use the DevAI inbox.

Do not include passwords, API keys, access tokens, session cookies, private keys,
or other secrets. DevAI stores the conversation as a GitHub issue and comments in
the configured support repository, where authorized support engineers,
configured issue owners, and repository maintainers can read it.

## Continue the conversation

Open a thread and use **Add a reply** to provide another result, answer a support
question, or add reproduction details. Both the submitter and support can reply
while the thread is open. The inbox refreshes after each reply and keeps the
newest activity first. Reload or revisit **Feedback** to pick up a response added
after the page was opened.

Users see only requests owned by their signed-in, tenant-qualified identity.
Support engineers, administrators, and configured issue owners see the support
inbox and can open all feedback threads. A request belonging to another user or
tenant is returned as not found rather than revealing that it exists.

## Resolution and reopening

GitHub issue state is the source of truth for the thread status:

| Status | User | Support engineer or configured owner |
|---|---|---|
| Open | Read and reply | Read, reply, and select **Mark resolved** |
| Resolved | Read the complete conversation | Read and select **Reopen thread** |

Replies are rejected after resolution, so the conversation cannot silently
continue on a closed request. Ask a support engineer or configured owner to
reopen the thread when more work or information is required.

## What to include

For a bug report, include:

- the action being attempted;
- the expected and actual result;
- the approximate time and affected DevAI area;
- safe reproduction steps; and
- a trace, run, sandbox, or evaluation identifier when relevant.

For a requirement or improvement, include the user outcome, current limitation,
acceptance criteria, and any safety or compatibility constraints.

## Troubleshooting

| Result | Meaning | Action |
|---|---|---|
| Redirected to login or `401` | The dashboard session is missing or expired | Sign in again and reopen **Feedback** |
| Thread not found | The ID is invalid, is not feedback, or belongs to another identity | Check the selected account and tenant; do not retry IDs from another user |
| Thread is closed | Support has resolved the backing issue | Ask support to reopen it before replying |
| Validation error | A required field is empty or exceeds its limit | Use a title of at most 200 characters and details/replies of at most 10,000 characters |
| Rate limited | Too many submissions or replies were sent in a short period | Wait before retrying; do not submit duplicates |

For CLI installation or session troubleshooting, see
[Install and authenticate the DevAI CLI](install-and-authenticate.md).
