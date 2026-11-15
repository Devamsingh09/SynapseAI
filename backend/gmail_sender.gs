/**
 * Synapse AI mail sender — a Google Apps Script web app.
 *
 * The backend (email_service.py) POSTs {secret, to, subject, html, name,
 * replyTo} and this sends the email from the Gmail account that deployed it.
 * Hugging Face Spaces can't reach SMTP servers, so this runs on Google's side
 * and is called over HTTPS. Free Gmail accounts can send to ~100 recipients/day.
 *
 * Setup (once):
 *  1. script.google.com → New project → paste this file → set SECRET below
 *     to the same value as the Space secret GMAIL_SCRIPT_SECRET.
 *  2. Run `testSend` once (grants permission; emails you a test).
 *  3. Deploy → New deployment → Web app → Execute as: Me,
 *     Who has access: Anyone → Deploy → copy the Web app URL into the Space
 *     secret GMAIL_SCRIPT_URL.
 *  After editing the script: Deploy → Manage deployments → edit → New version.
 */
const SECRET = "PASTE_THE_SAME_VALUE_AS_GMAIL_SCRIPT_SECRET";

function doPost(e) {
  let body;
  try {
    body = JSON.parse(e.postData.contents);
  } catch (err) {
    return reply({ ok: false, error: "bad request" });
  }
  if (!SECRET || SECRET.indexOf("PASTE_") === 0 || body.secret !== SECRET) {
    return reply({ ok: false, error: "unauthorized" });
  }
  if (!body.to || !body.subject || !body.html) {
    return reply({ ok: false, error: "missing to/subject/html" });
  }
  try {
    const message = { to: body.to, subject: body.subject, htmlBody: body.html, name: body.name || "Synapse AI" };
    if (body.replyTo) message.replyTo = body.replyTo;
    MailApp.sendEmail(message);
    return reply({ ok: true, remaining: MailApp.getRemainingDailyQuota() });
  } catch (err) {
    return reply({ ok: false, error: String(err) });
  }
}

// Opening the URL in a browser just confirms it's deployed.
function doGet() {
  return reply({ ok: true, service: "synapse-mail" });
}

function reply(obj) {
  return ContentService.createTextOutput(JSON.stringify(obj)).setMimeType(ContentService.MimeType.JSON);
}

// Run once from the editor: grants the "send email" permission and emails you a test.
function testSend() {
  MailApp.sendEmail({
    to: Session.getActiveUser().getEmail(),
    subject: "Synapse AI mail test",
    htmlBody: "Your Synapse AI mail sender works. Daily quota left: " + MailApp.getRemainingDailyQuota(),
    name: "Synapse AI",
  });
}
