# RecoverFlow product roadmap

## Positioning

RecoverFlow is a **receivables operating system for Indian SMBs**. It should sit on top of the accounting/ERP system instead of trying to replace Tally, Zoho Books, Vyapar or the customer's existing ledger.

Core promise:

> Import open receivables → know what needs attention → take the next approved collection action → record the outcome → keep working the invoice until cash arrives.

## Why this category

YC has funded multiple companies in accounts receivable, collections and order-to-cash, including yBANQ (collections + reconciliation for Indian MSMEs, W2020, later acquired), Upflow (cash collection for B2B, W2020), Levers (AR management SaaS, S2022), Fazeshift (AI agent for AR, S2024), Alder (AI employees for finance starting with AR, F2025), FullSeam (AI agents for accounting/AR/AP/reconciliation, W2026), and Rex (AI-native order-to-cash, S2026).

This makes the category YC-legible, but it also means RecoverFlow needs evidence of differentiated execution and customer pull rather than another generic reminder/invoicing product.

## MVP: Collections Autopilot

### 1. Receivable intake
- CSV import of open invoices
- Preserve invoice/customer/GST/TDS context
- Future: Tally, Zoho Books, Busy and ERP integrations

### 2. Collection Command Center
- Outstanding and overdue cash
- Due in 7 days
- Active promises-to-pay
- Broken promises
- Prioritized collection queue

### 3. Next-action engine
Rules v0 ranks invoices using:
- overdue age
- open balance
- broken promise
- dispute/exception
- payment claim

Each open invoice receives a human-readable next action such as:
- Send overdue reminder
- WhatsApp + payment link
- Call + payment link
- Resolve dispute
- Verify payment claim
- Follow up on broken promise

### 4. Structured customer outcomes
Record:
- contact attempt
- promise to pay
- dispute / exception
- payment claimed
- internal note

A promise records both promised date and amount.

### 5. Collection actions
- Existing RecoverFlow WhatsApp workflow
- Existing Razorpay payment links
- Existing invoice payment verification/webhook flow
- Existing mark-paid workflow

The MVP deliberately keeps financial execution human-approved.

### 6. Learning loop
Every collection activity becomes structured history that can later power:
- customer payment behavior
- risk signals
- next-best-action
- collection forecasting
- cash-flow forecasting

## Product map

Invoice / ERP
    ↓
Receivable intake
    ↓
Collection Command Center
    ↓
Priority + next action
    ├─→ WhatsApp
    ├─→ Call
    ├─→ Payment link
    └─→ Resolve exception
    ↓
Customer response
    ↓
Promise / dispute / payment claim
    ↓
Payment
    ↓
Webhook / verification
    ↓
Invoice closed
    ↓
Collection history
    ↓
Better next action

## Explicit non-goals for MVP

- Do not rebuild accounting
- Do not build inventory/payroll/expense management
- Do not add lending before collection workflow has strong usage
- Do not make AI autonomous for payments or ledger closure
- Do not add many integrations before one customer segment demonstrates repeated usage

## Initial ICP

Start with Indian B2B companies that:
- have a finance/collections operator
- maintain meaningful outstanding receivables
- use Tally/Zoho/Excel or another existing ledger
- communicate heavily over WhatsApp
- have enough invoices that manual follow-up becomes operationally painful

A practical pilot profile is roughly 10–100 employees and 50–1,000 open invoices, but customer interviews should determine the final ICP.

## Success metrics

The MVP should be measured by business outcomes, not feature count:

1. Time from import to first collection action
2. % of open invoices with a recorded next action
3. Promise-to-pay capture rate
4. Promise kept vs broken rate
5. Overdue balance recovered
6. Median days from first follow-up to payment
7. Weekly active finance/collections users
8. Pilot-to-paid conversion
9. Expansion in receivables managed per workspace

## YC evidence target

Before applying, aim to replace product claims with customer evidence:
- real businesses actively using the queue
- recurring weekly usage
- invoices actually recovered through the workflow
- quantified reduction in manual collection work
- retained customers
- paying customers where possible
- clear explanation of why existing accounting software is insufficient

The product does not need to be unique in the sense that nobody else has built it. It needs to solve a painful problem, show user pull, and demonstrate why the team can build a large company around the opportunity.

## Research references

- Y Combinator application guidance: https://www.ycombinator.com/howtoapply
- Y Combinator FAQ: https://www.ycombinator.com/faq
- Y Combinator Upflow: https://www.ycombinator.com/companies/upflow
- Y Combinator Levers: https://www.ycombinator.com/companies/levers
- Y Combinator Fazeshift: https://www.ycombinator.com/companies/fazeshift
- Y Combinator Alder: https://www.ycombinator.com/companies/alder
- Y Combinator FullSeam: https://www.ycombinator.com/companies/fullseam
- Y Combinator Rex: https://www.ycombinator.com/companies/rex-inc
- Y Combinator yBANQ: https://www.ycombinator.com/companies/ybanq


## Collections OS v2

The next product layer introduces first-class customers, collection cases and collection tasks. The **Today** workspace turns the receivables queue into a daily operating list with a reason, action type, deadline and outcome log.

The import path now accepts CSV/XLSX and validates the tabular file before database ingestion. See `docs/OSS_STACK.md` for the open-source components selected after reviewing current GitHub projects and licenses.
