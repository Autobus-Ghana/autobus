# Building a custom workflow

A business asks for an automation from the Autobus portal. The back office reads that request in the admin portal. If it is feasible, you build it in AI Workflow and publish it to the hosted n8n instance. The workflow can call the agents that already exist: business intelligence, customer service, digital marketing, and multi-account posting.

## 1. What the business submits

In the business portal, open **Agents** and choose **Request a custom workflow**.

The form asks what the business sells, which capabilities to use, when the workflow should run, and what a good result looks like. Submitting it creates a row in **Automation Requests** with status **Submitted**. The answers are stored on that row, including:

| Answer | What you use it for |
| --- | --- |
| Modules | Which agent nodes to put on the canvas |
| Trigger | Webhook, schedule, or manual start |
| Business user id | The merchant the agents run as |

A request is feasible when the outcome can be done with those four agents, plus the triggers and actions already in the builder (email, HTTP, a condition, and so on). If it needs a system Autobus does not talk to, mark it **Rejected** or **Blocked** and say why in the status you keep with the team.

## 2. Read the request

In the admin portal, open **Automation Requests**.

1. Set the status to **Triaged** while you read it, then **Approved** if you will build it.
2. Open the row. The survey block is the brief. **Business user** is the id you put on every Autobus node.
3. Choose **Build workflow**.

That creates a draft in **AI Workflow**, named `Custom: <business>`, and moves the request to **In progress**. The canvas already has the trigger they asked for and one node per module they selected. The workflow is inactive until you push it and turn it on.

Open **AI Workflow**, find that draft under the business name, and edit it.

## 3. The four Autobus nodes

These nodes are in the palette under **Autobus modules**. Each one calls the live agent for that merchant. Put the business user id from the request on every node. Text fields that start with `={{` read a value from the previous step.

| Node | What it does | What comes back |
| --- | --- | --- |
| Business intelligence | Answers a question from the merchant’s profile, products, orders, inbox, and uploaded knowledge | `answer` |
| Customer service | Drafts a reply with the same agent that handles inbox and WhatsApp chats | `reply` |
| Create marketing content | Writes a caption or campaign. It does not publish | `content` |
| Post to connected accounts | Publishes that caption to Postiz accounts you name | `postiz` |

Chain them when the brief needs more than one. A typical marketing workflow is:

1. Trigger (a webhook with the brief, or a schedule).
2. **Create marketing content**, with the brief in **Brief**.
3. **Post to connected accounts**, with caption `={{ $json.content }}` and the account ids for that merchant.

Customer service usually stops at the reply. The next node can send `={{ $json.reply }}` through the channel the brief names (WhatsApp, email, or an HTTP call). The customer-service node writes the reply. It does not send it by itself.

Intelligence is for the owner, not the customer. Use it for a morning digest, a stock question, or a check before you post.

### Account ids for posting

Posting needs the Postiz integration id of each channel, separated by commas. With the workflow key set, you can list them:

```bash
curl -sS "https://api.useautobus.com/api/v1/workflow-actions/social-accounts?businessUserId=THE_USER_ID" \
  -H "X-Workflow-Key: $WORKFLOW_ACTION_KEY"
```

Copy the `id` values into **Account ids**. Leave **Media URLs** empty for a text post, or paste public image or video URLs separated by commas. TikTok and YouTube need a video URL. **Publish immediately** sends the post when the workflow runs. Turn it off only when you also have a schedule in the request body.

Each of the text agents spends one LLM credit on the merchant’s wallet. If the wallet is empty, that step fails and n8n shows the error.

## 4. Publish to n8n

1. In the builder, check the business user id and the account ids.
2. Push the workflow to n8n (the toolbar sync). The admin portal never sends the n8n API key to the browser. The API holds it.
3. Open the n8n editor from the builder if you need a credential n8n owns (Gmail, Slack, Sheets). Autobus nodes do not need an n8n credential. They send `X-Workflow-Key` from the n8n environment.
4. Run it once with test data. For a webhook trigger, use **Listen for test event** in n8n, then run the test from the builder.
5. Read the execution. Intelligence should return `answer`, customer service `reply`, marketing `content`.
6. Activate the workflow only after that test. Then set the automation request to **Delivered**.

The webhook path is on the trigger node, for example `custom-<request id>`. Production calls are:

```text
POST https://n8n.useautobus.com/webhook/<path>
```

## 5. Values the servers need

Set these before the first push. Use the same secret for both keys. Do not put that secret in the business portal or in a ticket.

| Where | Name | Purpose |
| --- | --- | --- |
| Autobus API | `WORKFLOW_ACTION_KEY` | Checks `X-Workflow-Key` on `/api/v1/workflow-actions/*` |
| Autobus API | `N8N_API_KEY` | Lets the admin portal create and update workflows |
| Autobus API | `N8N_BASE_URL` | n8n on the Docker network, usually `http://n8n:5678` |
| Autobus API | `N8N_PUBLIC_URL` | Editor link, `https://n8n.useautobus.com` |
| n8n | `AUTOBUS_WORKFLOW_KEY` | Same secret as `WORKFLOW_ACTION_KEY` |
| n8n | `N8N_BLOCK_ENV_ACCESS_IN_NODE` | Set to `false` so a node can read `AUTOBUS_WORKFLOW_KEY` |

If the public API is not `https://api.useautobus.com`, set `workflowActionsUrl` in the admin build to `https://<api-host>/api/v1/workflow-actions`. The Autobus nodes call that host.

The action routes are:

```text
POST /api/v1/workflow-actions/intelligence
POST /api/v1/workflow-actions/customer-service
POST /api/v1/workflow-actions/marketing-content
POST /api/v1/workflow-actions/social-post
GET  /api/v1/workflow-actions/social-accounts?businessUserId=...
```

Every call sends `X-Workflow-Key`. A missing key on the API returns 503. A wrong key returns 401.

## 6. Three patterns that cover most requests

**Reply to a customer message.** Webhook trigger whose body includes `message`. Customer service node. Then an email, WhatsApp, or HTTP node that sends `={{ $json.reply }}`.

**Monday digest for the owner.** Schedule trigger. Business intelligence node with a fixed question, such as “What is open in the inbox and which products are low on stock?” Send `={{ $json.answer }}` to the owner’s email.

**Write a post and publish it.** Webhook or manual trigger with the brief in `message`. Create marketing content. Post to connected accounts with caption `={{ $json.content }}` and the account ids from the curl above.

Add an **If** node when the brief says to skip a step, for example “only post if the caption mentions a price.”

## 7. When to stop

- Do not activate a workflow that posts to social accounts until you have seen one successful test execution.
- Do not point a node at a user id that is not on the request.
- Do not paste the workflow key into a node as plain text. The node already reads it from the n8n environment.
- If the request needs a tool that is not in the palette, leave the request **Blocked** and build that tool in Autobus first. Then add an HTTP node, or a new Autobus module, and come back to the request.
