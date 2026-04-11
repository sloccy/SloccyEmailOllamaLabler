package processor

import (
	"context"
	"encoding/json"
	"log/slog"

	"github.com/sloccy/ollamail/db"
	gmailpkg "github.com/sloccy/ollamail/gmail"
	"github.com/sloccy/ollamail/llm"
)

// BackfillLlmDebug seeds the llm_debug table with the 3 most recent processed
// emails when the table is empty on boot. It re-fetches each message from Gmail
// and rebuilds the LLM request JSON locally (no LLM call). llm_response is
// taken from categorization_history. If the table already has any rows, this is
// a no-op.
func BackfillLlmDebug(ctx context.Context, store *db.Store, ollamaClient *llm.Client, gmailAuth *gmailpkg.Auth, cfg ProcessConfig) error {
	existing, err := store.GetLatestLlmDebug(ctx)
	if err != nil {
		return err
	}
	if len(existing) > 0 {
		return nil
	}

	history, err := store.GetHistory(ctx, 3)
	if err != nil {
		return err
	}
	if len(history) == 0 {
		return nil
	}

	oauthCfg, err := gmailAuth.ConfigFromFile()
	if err != nil {
		return err
	}

	allPrompts, err := store.ListPrompts(ctx)
	if err != nil {
		return err
	}

	// Cache gmail clients per account to avoid creating multiple services.
	type svcEntry struct {
		svc     *gmailpkg.Client
		prompts []llm.Prompt
	}
	svcCache := map[int64]*svcEntry{}

	// history is id DESC (newest first); insert oldest-first so the newest
	// history row gets the highest llm_debug id.
	for i := len(history) - 1; i >= 0; i-- {
		h := history[i]

		entry, ok := svcCache[h.AccountID]
		if !ok {
			account, err := store.GetAccount(ctx, h.AccountID)
			if err != nil {
				slog.Warn("backfill: get account", "account_id", h.AccountID, "err", err)
				entry = &svcEntry{}
			} else {
				svc, err := gmailpkg.NewService(ctx, account.CredentialsJson, oauthCfg, func(newCreds string) {
					_ = store.UpdateAccountCredentials(ctx, db.UpdateAccountCredentialsParams{
						CredentialsJson: newCreds,
						ID:              account.ID,
					})
				})
				if err != nil {
					slog.Warn("backfill: gmail service", "account", account.Email, "err", err)
					entry = &svcEntry{}
				} else {
					dbPrompts := filterPrompts(allPrompts, account.ID)
					llmPrompts := make([]llm.Prompt, len(dbPrompts))
					for j, p := range dbPrompts {
						llmPrompts[j] = llm.Prompt{ID: p.ID, Name: p.Name, Instructions: p.Instructions}
					}
					entry = &svcEntry{svc: svc, prompts: llmPrompts}
				}
			}
			svcCache[h.AccountID] = entry
		}

		var gmailRaw, llmRequest string

		if entry.svc != nil {
			msg, err := gmailpkg.FetchMessage(ctx, entry.svc, h.MessageID, cfg.BodyTruncation)
			if err != nil {
				slog.Warn("backfill: fetch message", "message_id", h.MessageID, "err", err)
			} else {
				rawBytes, err := json.MarshalIndent(msg, "", "  ")
				if err != nil {
					rawBytes = []byte("{}")
				}
				gmailRaw = string(rawBytes)

				if len(entry.prompts) > 0 {
					email := llm.Email{
						Sender:  msg.Sender,
						Subject: msg.Subject,
						Body:    msg.Body,
						Snippet: msg.Snippet,
					}
					llmRequest = ollamaClient.BuildClassifyRequestJSON(email, entry.prompts)
				}
			}
		}

		if err := store.AddLlmDebug(ctx, db.AddLlmDebugParams{
			AccountID:    h.AccountID,
			AccountEmail: h.AccountEmail,
			MessageID:    h.MessageID,
			Subject:      h.Subject,
			Sender:       h.Sender,
			GmailRaw:     gmailRaw,
			LlmRequest:   llmRequest,
			LlmResponse:  h.LlmResponse,
		}); err != nil {
			slog.Warn("backfill: insert llm_debug", "message_id", h.MessageID, "err", err)
		}
	}

	return store.TrimLlmDebug(ctx)
}
