-- Add webhook_hook_id to repos table for tracking auto-registered GitHub webhooks
ALTER TABLE repos ADD COLUMN IF NOT EXISTS webhook_hook_id INTEGER;
