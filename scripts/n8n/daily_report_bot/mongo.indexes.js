// Run once in mongosh against your n8n MongoDB database
// use your_db_name

db.daily_reports.createIndex({ report_id: 1 }, { unique: true });
db.daily_reports.createIndex({ report_date: 1, summarized: 1 });
db.daily_reports.createIndex({ sender_phone: 1, report_date: -1 });

// Example document (written by the n8n MongoDB Insert node):
// {
//   report_id: "RPT-F3556DD114",
//   sender_phone: "233241234567",
//   sender_name: "Ama",
//   phone_number_id: "12345",
//   report_date: "2026-10-26",
//   body: "Finished NLU extract...",
//   done: "",
//   blockers: "",
//   plans: "",
//   intent: "submit_daily_report",
//   confidence: 0.95,
//   wa_message_id: "wamid.1",
//   created_at: "2026-10-26T18:00:00+00:00",
//   summarized: false
// }
