package main

import (
	"compress/gzip"
	"context"
	"crypto/rand"
	"database/sql"
	"embed"
	"encoding/csv"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"html/template"
	"io/fs"
	"log/slog"
	"net/http"
	"net/url"
	"strconv"
	"strings"
	"sync"
	"time"

	"github.com/sloccy/ollamail/db"
	"github.com/sloccy/ollamail/gmail"
	"github.com/sloccy/ollamail/llm"
	"github.com/sloccy/ollamail/poller"
)

//go:embed static
var staticFS embed.FS

const retentionUnitYears = "years"

// server holds all dependencies and the route mux.
type server struct {
	store     *db.Store
	ollama    *llm.Client
	poller    *poller.Poller
	cfg       *Config
	auth      *gmail.Auth
	secretKey []byte
	tmpl      *template.Template
	mux       *http.ServeMux

	// OAuth state: short-lived in-memory map (single instance, no need for persistent storage)
	oauthMu    sync.Mutex
	oauthState map[string]time.Time
}

func newServer(store *db.Store, ollamaClient *llm.Client, p *poller.Poller, auth *gmail.Auth, cfg *Config, secretKey []byte) http.Handler {
	s := &server{
		store:      store,
		ollama:     ollamaClient,
		poller:     p,
		cfg:        cfg,
		auth:       auth,
		secretKey:  secretKey,
		oauthState: make(map[string]time.Time),
	}

	var err error
	s.tmpl, err = loadTemplates()
	if err != nil {
		panic(fmt.Sprintf("load templates: %v", err))
	}

	s.mux = http.NewServeMux()
	s.registerRoutes()
	return s
}

func (s *server) ServeHTTP(w http.ResponseWriter, r *http.Request) {
	s.mux.ServeHTTP(w, r)
}

func (s *server) registerRoutes() {
	// Static
	staticSub, _ := fs.Sub(staticFS, "static")
	s.mux.Handle("GET /static/", http.StripPrefix("/static/", http.FileServer(http.FS(staticSub))))

	// Index
	s.mux.HandleFunc("GET /", func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/" {
			http.NotFound(w, r)
			return
		}
		s.renderPage(w, "index.html", nil)
	})

	// Fragments
	s.mux.HandleFunc("GET /fragments/dashboard", s.handleDashboard)
	s.mux.HandleFunc("GET /fragments/accounts", s.handleAccounts)
	s.mux.HandleFunc("POST /fragments/accounts/{id}/toggle", s.handleToggleAccount)
	s.mux.HandleFunc("DELETE /fragments/accounts/{id}", s.handleDeleteAccount)
	s.mux.HandleFunc("GET /fragments/prompts", s.handlePromptsList)
	s.mux.HandleFunc("POST /fragments/prompts", s.handleCreatePrompt)
	s.mux.HandleFunc("PUT /fragments/prompts/{id}", s.handleUpdatePrompt)
	s.mux.HandleFunc("DELETE /fragments/prompts/{id}", s.handleDeletePrompt)
	s.mux.HandleFunc("POST /fragments/prompts/{id}/toggle", s.handleTogglePrompt)
	s.mux.HandleFunc("GET /fragments/prompts/{id}/edit", s.handleEditPrompt)
	s.mux.HandleFunc("GET /fragments/prompts/{id}/view", s.handleViewPrompt)
	s.mux.HandleFunc("GET /fragments/settings", s.handleGetSettings)
	s.mux.HandleFunc("PATCH /fragments/settings", s.handleUpdateSettings)
	s.mux.HandleFunc("GET /fragments/logs", s.handleLogs)
	s.mux.HandleFunc("GET /fragments/history", s.handleHistory)
	s.mux.HandleFunc("GET /fragments/history/filters", s.handleHistoryFilters)
	s.mux.HandleFunc("GET /fragments/retention/{id}", s.handleGetRetention)
	s.mux.HandleFunc("POST /fragments/retention/{id}", s.handleSetGlobalRetention)
	s.mux.HandleFunc("POST /fragments/retention/{id}/labels", s.handleAddLabelRetention)
	s.mux.HandleFunc("DELETE /fragments/retention/{id}/labels/{ruleId}", s.handleDeleteLabelRetention)
	s.mux.HandleFunc("POST /fragments/retention/{id}/exemptions", s.handleAddExemption)
	s.mux.HandleFunc("DELETE /fragments/retention/{id}/exemptions/{eid}", s.handleDeleteExemption)
	s.mux.HandleFunc("POST /fragments/oauth/start", s.handleOAuthStart)
	s.mux.HandleFunc("POST /fragments/oauth/exchange", s.handleOAuthExchange)
	s.mux.HandleFunc("POST /fragments/scan", s.handleScan)
	s.mux.HandleFunc("GET /fragments/account-options", s.handleAccountOptions)
	s.mux.HandleFunc("GET /fragments/retention-query", s.handleRetentionQuery)

	// JSON APIs
	s.mux.HandleFunc("POST /api/prompts/reorder", s.handleReorderPrompts)
	s.mux.HandleFunc("GET /api/prompts/export", s.handleExportPrompts)
	s.mux.HandleFunc("GET /api/config/export", s.handleExportConfig)
	s.mux.HandleFunc("POST /api/config/import", s.handleImportConfig)
	s.mux.HandleFunc("GET /api/logs/download", s.handleDownloadLogs)
	s.mux.HandleFunc("GET /api/prompts/generate-stream", s.handleGenerateStream)
}

// ============================================================
// Template rendering helpers
// ============================================================

func (s *server) renderPage(w http.ResponseWriter, name string, data any) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if err := s.tmpl.ExecuteTemplate(w, name, data); err != nil {
		slog.Error("render page", "name", name, "err", err)
	}
}

func (s *server) renderFragment(w http.ResponseWriter, name string, data any) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	if err := s.tmpl.ExecuteTemplate(w, name, data); err != nil {
		slog.Error("render fragment", "name", name, "err", err)
	}
}

// renderFragmentFile renders a pre-parsed fragment template by its base filename.
func (s *server) renderFragmentFile(w http.ResponseWriter, r *http.Request, path string, data any) {
	name := path[strings.LastIndex(path, "/")+1:]
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	var buf strings.Builder
	if err := s.tmpl.ExecuteTemplate(&buf, name, data); err != nil {
		slog.Error("execute fragment", "name", name, "err", err)
		http.Error(w, "render error", 500)
		return
	}
	s.writeGzip(w, r, []byte(buf.String()))
}

func (s *server) writeGzip(w http.ResponseWriter, r *http.Request, body []byte) {
	if len(body) >= 500 && strings.Contains(r.Header.Get("Accept-Encoding"), "gzip") {
		w.Header().Set("Content-Encoding", "gzip")
		gz := gzip.NewWriter(w)
		defer func() { _ = gz.Close() }()
		_, _ = gz.Write(body)
	} else {
		_, _ = w.Write(body)
	}
}

func (s *server) fragmentResponse(w http.ResponseWriter, r *http.Request, path string, data any, toast string) {
	if toast != "" {
		triggers := map[string]any{"showToast": toast}
		if b, err := json.Marshal(triggers); err == nil {
			w.Header().Set("HX-Trigger", string(b))
		}
	}
	s.renderFragmentFile(w, r, path, data)
}

func (s *server) fragmentResponseNamed(w http.ResponseWriter, _ *http.Request, name string, data any) {
	s.renderFragment(w, name, data)
}

// ============================================================
// Dashboard
// ============================================================

func (s *server) handleDashboard(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	accounts, _ := s.store.ListAccountsSafe(ctx)
	activePrompts, _ := s.store.CountActivePrompts(ctx)
	logs, _ := s.store.GetLogs(ctx, 100)
	status := s.poller.GetStatus()
	pollIntervalSetting, _ := s.store.GetSetting(ctx, "poll_interval")
	pollSecs, _ := strconv.Atoi(pollIntervalSetting)

	nextScan := "--"
	if status.NextRun != "" {
		if t, err := time.Parse("2006-01-02 15:04:05", status.NextRun); err == nil {
			nextScan = t.Format("15:04:05")
		}
	}

	data := map[string]any{
		"PollerRunning": status.Running,
		"AccountCount":  len(accounts),
		"ActivePrompts": activePrompts,
		"PollInterval":  fmtinterval(pollSecs),
		"NextScan":      nextScan,
		"Logs":          logs,
	}
	s.fragmentResponse(w, r, "templates/fragments/dashboard.html", data, "")
}

// ============================================================
// Accounts
// ============================================================

type accountView struct {
	ID         int64
	Email      string
	Active     bool
	AddedAt    string
	LastScanAt string
}

func (s *server) handleAccounts(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	rows, _ := s.store.ListAccountsSafe(ctx)
	views := make([]accountView, len(rows))
	for i, a := range rows {
		views[i] = accountView{
			ID:         a.ID,
			Email:      a.Email,
			Active:     a.Active != 0,
			AddedAt:    a.AddedAt,
			LastScanAt: a.LastScanAt.String,
		}
	}
	s.fragmentResponse(w, r, "templates/fragments/accounts_list.html", views, "")
}

func (s *server) handleToggleAccount(w http.ResponseWriter, r *http.Request) {
	id := pathInt(r, "id")
	if id == 0 {
		http.Error(w, "bad id", 400)
		return
	}
	ctx := r.Context()
	_, _ = s.store.ToggleAccount(ctx, id)
	s.handleAccounts(w, r)
}

func (s *server) handleDeleteAccount(w http.ResponseWriter, r *http.Request) {
	id := pathInt(r, "id")
	if id == 0 {
		http.Error(w, "bad id", 400)
		return
	}
	ctx := r.Context()
	_ = s.store.DeleteAccountCascade(ctx, id)
	s.handleAccounts(w, r)
}

// ============================================================
// Prompts
// ============================================================

type promptView struct {
	ID             int64
	Name           string
	Instructions   string
	LabelName      string
	Active         bool
	CreatedAt      string
	ActionArchive  bool
	ActionSpam     bool
	ActionTrash    bool
	ActionMarkRead bool
	StopProcessing bool
	AccountID      int64
	AccountEmail   string
}

type promptEditView struct {
	Prompt   promptView
	Accounts []accountView
}

func (s *server) getPromptViews(ctx context.Context, accountIDFilter string) ([]promptView, error) {
	var prompts []db.Prompt
	var err error
	if accountIDFilter != "" && accountIDFilter != "0" {
		id, _ := strconv.ParseInt(accountIDFilter, 10, 64)
		prompts, err = s.store.ListPromptsByAccount(ctx, sql.NullInt64{Int64: id, Valid: true})
	} else {
		prompts, err = s.store.ListPrompts(ctx)
	}
	if err != nil {
		return nil, err
	}

	accounts, _ := s.store.ListAccountsSafe(ctx)
	accountMap := buildAccountMap(accounts)

	views := make([]promptView, len(prompts))
	for i, p := range prompts {
		views[i] = dbPromptToView(p, accountMap)
	}
	return views, nil
}

func (s *server) handlePromptsList(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	accountFilter := r.URL.Query().Get("account_id")
	views, _ := s.getPromptViews(ctx, accountFilter)
	s.fragmentResponse(w, r, "templates/fragments/prompts_list.html", views, "")
}

func (s *server) handleCreatePrompt(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	_ = r.ParseForm()

	name := strings.TrimSpace(r.FormValue("name"))
	labelName := strings.TrimSpace(r.FormValue("label_name"))
	instructions := strings.TrimSpace(r.FormValue("instructions"))
	if name == "" {
		s.fragmentResponse(w, r, "templates/fragments/prompts_list.html", nil, "Name is required")
		return
	}

	var accountID sql.NullInt64
	if v := r.FormValue("account_id"); v != "" {
		if id, err := strconv.ParseInt(v, 10, 64); err == nil {
			accountID = sql.NullInt64{Int64: id, Valid: true}
		}
	}

	maxOrderRaw, _ := s.store.MaxPromptSortOrder(ctx)
	var maxOrder int64
	if v, ok := maxOrderRaw.(int64); ok {
		maxOrder = v
	}
	_, err := s.store.CreatePrompt(ctx, db.CreatePromptParams{
		Name:           name,
		Instructions:   instructions,
		LabelName:      labelName,
		ActionArchive:  boolToInt(r.FormValue("action_archive") == "1"),
		ActionSpam:     boolToInt(r.FormValue("action_spam") == "1"),
		ActionTrash:    boolToInt(r.FormValue("action_trash") == "1"),
		ActionMarkRead: boolToInt(r.FormValue("action_mark_read") == "1"),
		SortOrder:      maxOrder + 1,
		StopProcessing: boolToInt(r.FormValue("stop_processing") == "1"),
		AccountID:      accountID,
	})
	if err != nil {
		slog.Error("create prompt", "err", err)
		s.fragmentResponse(w, r, "templates/fragments/prompts_list.html", nil, "Failed to create rule")
		return
	}

	// Pre-create label in background for all matching accounts
	go s.ensureLabelForAccounts(context.Background(), labelName, accountID)

	views, _ := s.getPromptViews(ctx, "")
	s.fragmentResponse(w, r, "templates/fragments/prompts_list.html", views, "Rule saved")
}

func (s *server) handleUpdatePrompt(w http.ResponseWriter, r *http.Request) {
	id := pathInt(r, "id")
	if id == 0 {
		http.Error(w, "bad id", 400)
		return
	}
	ctx := r.Context()
	_ = r.ParseForm()

	var accountID sql.NullInt64
	if v := r.FormValue("account_id"); v != "" {
		if aid, err := strconv.ParseInt(v, 10, 64); err == nil {
			accountID = sql.NullInt64{Int64: aid, Valid: true}
		}
	}
	labelName := strings.TrimSpace(r.FormValue("label_name"))

	_ = s.store.UpdatePrompt(ctx, db.UpdatePromptParams{
		Name:           strings.TrimSpace(r.FormValue("name")),
		Instructions:   strings.TrimSpace(r.FormValue("instructions")),
		LabelName:      labelName,
		ActionArchive:  boolToInt(r.FormValue("action_archive") == "1"),
		ActionSpam:     boolToInt(r.FormValue("action_spam") == "1"),
		ActionTrash:    boolToInt(r.FormValue("action_trash") == "1"),
		ActionMarkRead: boolToInt(r.FormValue("action_mark_read") == "1"),
		StopProcessing: boolToInt(r.FormValue("stop_processing") == "1"),
		AccountID:      accountID,
		ID:             id,
	})

	go s.ensureLabelForAccounts(context.Background(), labelName, accountID)

	views, _ := s.getPromptViews(ctx, "")
	s.fragmentResponse(w, r, "templates/fragments/prompts_list.html", views, "Rule updated")
}

func (s *server) handleDeletePrompt(w http.ResponseWriter, r *http.Request) {
	id := pathInt(r, "id")
	ctx := r.Context()
	_ = s.store.DeletePrompt(ctx, id)
	views, _ := s.getPromptViews(ctx, "")
	s.fragmentResponse(w, r, "templates/fragments/prompts_list.html", views, "Rule deleted")
}

func (s *server) handleTogglePrompt(w http.ResponseWriter, r *http.Request) {
	id := pathInt(r, "id")
	ctx := r.Context()
	_, _ = s.store.TogglePrompt(ctx, id)

	p, err := s.store.GetPrompt(ctx, id)
	if err != nil {
		http.Error(w, "not found", 404)
		return
	}
	accounts, _ := s.store.ListAccountsSafe(ctx)
	pv := dbPromptToView(p, buildAccountMap(accounts))
	s.fragmentResponseNamed(w, r, "prompt_card_view", pv)
}

func (s *server) handleEditPrompt(w http.ResponseWriter, r *http.Request) {
	id := pathInt(r, "id")
	ctx := r.Context()
	p, err := s.store.GetPrompt(ctx, id)
	if err != nil {
		http.Error(w, "not found", 404)
		return
	}
	accounts, _ := s.store.ListAccountsSafe(ctx)
	accountViews := make([]accountView, len(accounts))
	for i, a := range accounts {
		accountViews[i] = accountView{ID: a.ID, Email: a.Email}
	}
	data := promptEditView{
		Prompt:   dbPromptToView(p, buildAccountMap(accounts)),
		Accounts: accountViews,
	}
	s.fragmentResponseNamed(w, r, "prompt_card_edit", data)
}

func (s *server) handleViewPrompt(w http.ResponseWriter, r *http.Request) {
	id := pathInt(r, "id")
	ctx := r.Context()
	p, err := s.store.GetPrompt(ctx, id)
	if err != nil {
		http.Error(w, "not found", 404)
		return
	}
	accounts, _ := s.store.ListAccountsSafe(ctx)
	s.fragmentResponseNamed(w, r, "prompt_card_view", dbPromptToView(p, buildAccountMap(accounts)))
}

func buildAccountMap(accounts []db.ListAccountsSafeRow) map[int64]string {
	m := make(map[int64]string, len(accounts))
	for _, a := range accounts {
		m[a.ID] = a.Email
	}
	return m
}

func dbPromptToView(p db.Prompt, accountMap map[int64]string) promptView {
	pv := promptView{
		ID:             p.ID,
		Name:           p.Name,
		Instructions:   p.Instructions,
		LabelName:      p.LabelName,
		Active:         p.Active != 0,
		CreatedAt:      p.CreatedAt,
		ActionArchive:  p.ActionArchive != 0,
		ActionSpam:     p.ActionSpam != 0,
		ActionTrash:    p.ActionTrash != 0,
		ActionMarkRead: p.ActionMarkRead != 0,
		StopProcessing: p.StopProcessing != 0,
	}
	if p.AccountID.Valid {
		pv.AccountID = p.AccountID.Int64
		pv.AccountEmail = accountMap[p.AccountID.Int64]
	}
	return pv
}

// ============================================================
// Settings
// ============================================================

func (s *server) handleGetSettings(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	pollInterval, _ := s.store.GetSetting(ctx, "poll_interval")
	pi, _ := strconv.Atoi(pollInterval)
	data := map[string]any{
		"PollInterval": pi,
		"OllamaModel":  s.cfg.OllamaModel,
		"OllamaHost":   s.cfg.OllamaHost,
	}
	s.fragmentResponse(w, r, "templates/fragments/settings_form.html", data, "")
}

func (s *server) handleUpdateSettings(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	_ = r.ParseForm()
	pi := r.FormValue("poll_interval")
	n, err := strconv.Atoi(pi)
	if err != nil || n < s.cfg.MinPollInterval {
		n = s.cfg.MinPollInterval
	}
	_ = s.store.SetSetting(ctx, db.SetSettingParams{Key: "poll_interval", Value: strconv.Itoa(n)})
	s.poller.UpdateInterval(n)

	data := map[string]any{
		"PollInterval": n,
		"OllamaModel":  s.cfg.OllamaModel,
		"OllamaHost":   s.cfg.OllamaHost,
	}
	s.fragmentResponse(w, r, "templates/fragments/settings_form.html", data, "Settings saved")
}

// ============================================================
// Logs
// ============================================================

func (s *server) handleLogs(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	logs, _ := s.store.GetLogs(ctx, 100)
	s.fragmentResponseNamed(w, r, "logs_list", logs)
}

// ============================================================
// History
// ============================================================

type historyRow struct {
	Timestamp    string
	AccountEmail string
	Subject      string
	Sender       string
	PromptName   string
	LabelName    string
	ExtraActions []string
	LlmResponse  string
}

func (s *server) handleHistory(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	q := r.URL.Query()

	filter := db.HistoryFilter{
		Limit: int64(s.cfg.HistoryMaxLimit),
	}
	if v := q.Get("account_id"); v != "" {
		if id, err := strconv.ParseInt(v, 10, 64); err == nil {
			filter.AccountID = &id
		}
	}
	if v := q.Get("prompt_id"); v == "none" {
		filter.Unmatched = true
	} else if v != "" {
		if id, err := strconv.ParseInt(v, 10, 64); err == nil {
			filter.PromptID = &id
		}
	}
	filter.SubjectQ = q.Get("subject")
	filter.SenderQ = q.Get("sender")

	rows, err := s.store.GetHistoryFiltered(ctx, filter)
	if err != nil {
		slog.Error("history query", "err", err)
		rows = nil
	}

	views := make([]historyRow, len(rows))
	for i, h := range rows {
		views[i] = historyRow{
			Timestamp:    h.Timestamp,
			AccountEmail: h.AccountEmail,
			Subject:      h.Subject,
			Sender:       h.Sender,
			PromptName:   h.PromptName.String,
			LabelName:    h.LabelName.String,
			LlmResponse:  h.LlmResponse,
		}
		if h.Actions != "" {
			views[i].ExtraActions = strings.Split(h.Actions, ",")
		}
	}
	s.fragmentResponse(w, r, "templates/fragments/history_table.html", views, "")
}

func (s *server) handleHistoryFilters(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	accounts, _ := s.store.ListAccountsSafe(ctx)
	prompts, _ := s.store.ListPrompts(ctx)

	type promptOption struct {
		ID   int64
		Name string
	}
	options := make([]promptOption, len(prompts))
	for i, p := range prompts {
		options[i] = promptOption{ID: p.ID, Name: p.Name}
	}

	accountViews := make([]accountView, len(accounts))
	for i, a := range accounts {
		accountViews[i] = accountView{ID: a.ID, Email: a.Email}
	}

	s.fragmentResponse(w, r, "templates/fragments/history_filters.html", map[string]any{
		"Accounts": accountViews,
		"Prompts":  options,
	}, "")
}

// ============================================================
// Retention
// ============================================================

type retentionPanelData struct {
	AccountID                   int64
	GlobalEnabled               bool
	GlobalValue                 string
	GlobalUnit                  string
	Exemptions                  []db.LabelExemption
	LabelRules                  []db.LabelRetention
	AvailableLabelsForExemption []string
	AvailableLabelsForRules     []string
}

func (s *server) buildRetentionData(ctx context.Context, accountID int64) retentionPanelData {
	data := retentionPanelData{AccountID: accountID, GlobalUnit: "days"}

	ret, err := s.store.GetAccountRetention(ctx, accountID)
	if err == nil && ret.GlobalDays.Valid {
		data.GlobalEnabled = true
		gd := ret.GlobalDays.Int64
		if gd >= 365 && gd%365 == 0 {
			data.GlobalUnit = retentionUnitYears
			data.GlobalValue = strconv.FormatInt(gd/365, 10)
		} else {
			data.GlobalValue = strconv.FormatInt(gd, 10)
		}
	}

	data.Exemptions, _ = s.store.GetLabelExemptions(ctx, accountID)
	data.LabelRules, _ = s.store.GetLabelRetention(ctx, accountID)

	// Build available labels (excluding already used ones)
	exemptSet := map[string]bool{}
	for _, e := range data.Exemptions {
		exemptSet[strings.ToLower(e.LabelName)] = true
	}
	ruleSet := map[string]bool{}
	for _, r := range data.LabelRules {
		ruleSet[strings.ToLower(r.LabelName)] = true
	}

	return data
}

func (s *server) handleGetRetention(w http.ResponseWriter, r *http.Request) {
	id := pathInt(r, "id")
	ctx := r.Context()
	data := s.buildRetentionDataWithGmail(ctx, id)
	s.fragmentResponse(w, r, "templates/fragments/retention_panel.html", data, "")
}

func (s *server) handleSetGlobalRetention(w http.ResponseWriter, r *http.Request) {
	id := pathInt(r, "id")
	ctx := r.Context()
	_ = r.ParseForm()

	if r.FormValue("enabled") == "1" {
		val, _ := strconv.ParseInt(r.FormValue("value"), 10, 64)
		unit := r.FormValue("unit")
		days := val
		if unit == retentionUnitYears {
			days = val * 365
		}
		if days > 0 {
			_ = s.store.SetGlobalRetention(ctx, db.SetGlobalRetentionParams{AccountID: id, GlobalDays: sql.NullInt64{Int64: days, Valid: true}})
		}
	} else {
		_ = s.store.ClearGlobalRetention(ctx, id)
	}
	data := s.buildRetentionDataWithGmail(ctx, id)
	s.fragmentResponse(w, r, "templates/fragments/retention_panel.html", data, "Saved")
}

func (s *server) handleAddLabelRetention(w http.ResponseWriter, r *http.Request) {
	id := pathInt(r, "id")
	ctx := r.Context()
	_ = r.ParseForm()
	label := strings.TrimSpace(r.FormValue("label_name"))
	val, _ := strconv.ParseInt(r.FormValue("value"), 10, 64)
	unit := r.FormValue("unit")
	days := val
	if unit == retentionUnitYears {
		days = val * 365
	}
	if label != "" && days > 0 {
		_ = s.store.AddLabelRetention(ctx, db.AddLabelRetentionParams{AccountID: id, LabelName: label, Days: days})
	}
	data := s.buildRetentionDataWithGmail(ctx, id)
	s.fragmentResponse(w, r, "templates/fragments/retention_panel.html", data, "Rule added")
}

func (s *server) handleDeleteLabelRetention(w http.ResponseWriter, r *http.Request) {
	id := pathInt(r, "id")
	ruleID := pathInt(r, "ruleId")
	ctx := r.Context()
	_ = s.store.DeleteLabelRetention(ctx, db.DeleteLabelRetentionParams{ID: ruleID, AccountID: id})
	data := s.buildRetentionDataWithGmail(ctx, id)
	s.fragmentResponse(w, r, "templates/fragments/retention_panel.html", data, "Rule removed")
}

func (s *server) handleAddExemption(w http.ResponseWriter, r *http.Request) {
	id := pathInt(r, "id")
	ctx := r.Context()
	_ = r.ParseForm()
	label := strings.TrimSpace(r.FormValue("label_name"))
	if label != "" {
		_ = s.store.AddLabelExemption(ctx, db.AddLabelExemptionParams{AccountID: id, LabelName: label})
	}
	data := s.buildRetentionDataWithGmail(ctx, id)
	s.fragmentResponse(w, r, "templates/fragments/retention_panel.html", data, "Exemption added")
}

func (s *server) handleDeleteExemption(w http.ResponseWriter, r *http.Request) {
	id := pathInt(r, "id")
	eid := pathInt(r, "eid")
	ctx := r.Context()
	_ = s.store.DeleteLabelExemption(ctx, db.DeleteLabelExemptionParams{ID: eid, AccountID: id})
	data := s.buildRetentionDataWithGmail(ctx, id)
	s.fragmentResponse(w, r, "templates/fragments/retention_panel.html", data, "Exemption removed")
}

func (s *server) handleRetentionQuery(w http.ResponseWriter, r *http.Request) {
	idStr := r.URL.Query().Get("account_id")
	if idStr == "" {
		w.WriteHeader(204)
		return
	}
	id, _ := strconv.ParseInt(idStr, 10, 64)
	if id == 0 {
		w.WriteHeader(204)
		return
	}
	ctx := r.Context()
	data := s.buildRetentionDataWithGmail(ctx, id)
	s.fragmentResponse(w, r, "templates/fragments/retention_panel.html", data, "")
}

func (s *server) buildRetentionDataWithGmail(ctx context.Context, accountID int64) retentionPanelData {
	data := s.buildRetentionData(ctx, accountID)

	// Try to fetch Gmail labels for the dropdown
	account, err := s.store.GetAccount(ctx, accountID)
	if err != nil {
		return data // graceful: no labels, return empty dropdowns
	}
	oauthCfg, err := s.auth.ConfigFromFile()
	if err != nil {
		return data // graceful: credentials unavailable
	}
	svc, err := gmail.NewService(ctx, account.CredentialsJson, oauthCfg, func(newCreds string) {
		_ = s.store.UpdateAccountCredentials(ctx, db.UpdateAccountCredentialsParams{
			CredentialsJson: newCreds, ID: account.ID,
		})
	})
	if err != nil {
		return data // graceful: oauth failure
	}
	labels, err := gmail.ListLabels(ctx, svc)
	if err != nil {
		return data // graceful: label fetch failure
	}

	exemptSet := map[string]bool{}
	for _, e := range data.Exemptions {
		exemptSet[strings.ToLower(e.LabelName)] = true
	}
	ruleSet := map[string]bool{}
	for _, r := range data.LabelRules {
		ruleSet[strings.ToLower(r.LabelName)] = true
	}

	for _, l := range labels {
		lower := strings.ToLower(l.Name)
		switch {
		case !exemptSet[lower] && !ruleSet[lower]:
			data.AvailableLabelsForExemption = append(data.AvailableLabelsForExemption, l.Name)
			data.AvailableLabelsForRules = append(data.AvailableLabelsForRules, l.Name)
		case !exemptSet[lower]:
			data.AvailableLabelsForExemption = append(data.AvailableLabelsForExemption, l.Name)
		case !ruleSet[lower]:
			data.AvailableLabelsForRules = append(data.AvailableLabelsForRules, l.Name)
		}
	}

	return data
}

// ============================================================
// OAuth
// ============================================================

func (s *server) handleOAuthStart(w http.ResponseWriter, r *http.Request) {
	state := generateToken(16)
	s.oauthMu.Lock()
	s.oauthState[state] = time.Now().Add(10 * time.Minute)
	s.oauthMu.Unlock()

	authURL, err := s.auth.GetAuthURL(state)
	if err != nil {
		http.Error(w, "Could not generate auth URL: "+err.Error(), 500)
		return
	}
	data := map[string]string{"AuthURL": authURL}
	s.fragmentResponse(w, r, "templates/fragments/oauth_step2.html", data, "")
}

func (s *server) handleOAuthExchange(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	_ = r.ParseForm()
	rawURL := r.FormValue("url")
	parsed, err := url.Parse(rawURL)
	if err != nil {
		s.fragmentResponse(w, r, "templates/fragments/accounts_list.html", nil, "Invalid URL")
		return
	}
	code := parsed.Query().Get("code")
	state := parsed.Query().Get("state")

	s.oauthMu.Lock()
	exp, ok := s.oauthState[state]
	if ok {
		delete(s.oauthState, state)
	}
	s.oauthMu.Unlock()

	if !ok || time.Now().After(exp) {
		s.fragmentResponse(w, r, "templates/fragments/accounts_list.html", nil, "OAuth state expired — try again")
		return
	}

	emailAddr, credJSON, err := s.auth.ExchangeCode(ctx, code)
	if err != nil {
		slog.Error("oauth exchange", "err", err)
		s.fragmentResponse(w, r, "templates/fragments/accounts_list.html", nil, "OAuth failed: "+err.Error())
		return
	}

	_, err = s.store.UpsertAccount(ctx, db.UpsertAccountParams{Email: emailAddr, CredentialsJson: credJSON})
	if err != nil {
		slog.Error("upsert account", "err", err)
	}

	s.handleAccounts(w, r)
}

// ============================================================
// Scan
// ============================================================

func (s *server) handleScan(w http.ResponseWriter, r *http.Request) {
	s.poller.RunNow()
	w.Header().Set("HX-Trigger", `{"showToast":"Scan triggered"}`)
	w.WriteHeader(200)
}

// ============================================================
// Account options (dropdown)
// ============================================================

func (s *server) handleAccountOptions(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	accounts, _ := s.store.ListAccountsSafe(ctx)

	optType := r.URL.Query().Get("type")
	var firstOption template.HTML
	switch optType {
	case "filter":
		firstOption = template.HTML(`<option value="">All accounts</option>`)
	case "retention":
		firstOption = template.HTML(`<option value="">Select account…</option>`)
	default:
		firstOption = template.HTML(`<option value="">All accounts (global)</option>`)
	}

	avs := make([]accountView, len(accounts))
	for i, a := range accounts {
		avs[i] = accountView{ID: a.ID, Email: a.Email}
	}
	s.fragmentResponse(w, r, "templates/fragments/account_options.html", map[string]any{
		"FirstOption": firstOption,
		"Accounts":    avs,
	}, "")
}

// ============================================================
// JSON APIs
// ============================================================

func (s *server) handleReorderPrompts(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	var body struct {
		OrderedIDs []int64 `json:"ordered_ids"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		http.Error(w, "bad body", 400)
		return
	}
	if err := s.store.ReorderPrompts(ctx, body.OrderedIDs); err != nil {
		http.Error(w, "reorder failed", 500)
		return
	}
	w.WriteHeader(200)
}

func (s *server) handleExportPrompts(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	prompts, _ := s.store.ListPrompts(ctx)
	w.Header().Set("Content-Disposition", "attachment; filename=prompts.json")
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(prompts) //nolint:musttag // sqlc-generated struct lacks json tags by design
}

type configExport struct {
	Accounts  []db.Account       `json:"accounts"`
	Prompts   []db.Prompt        `json:"prompts"`
	Settings  []db.Setting       `json:"settings"`
	Retention []accountRetExport `json:"retention"`
}

type accountRetExport struct {
	AccountEmail string              `json:"account_email"`
	GlobalDays   *int64              `json:"global_days,omitempty"`
	Labels       []db.LabelRetention `json:"labels"`
	Exemptions   []db.LabelExemption `json:"exemptions"`
}

func (s *server) handleExportConfig(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	accounts, _ := s.store.ListAccounts(ctx)
	prompts, _ := s.store.ListPrompts(ctx)
	allSettings, _ := s.store.GetAllSettings(ctx)
	var settings []db.Setting
	for _, s := range allSettings {
		if s.Key == "secret_key" {
			continue
		}
		settings = append(settings, s)
	}

	// Strip credentials from export
	safeAccounts := make([]db.Account, len(accounts))
	for i, a := range accounts {
		a.CredentialsJson = ""
		safeAccounts[i] = a
	}

	retentions := make([]accountRetExport, 0, len(accounts))
	for _, a := range accounts {
		entry := accountRetExport{AccountEmail: a.Email}
		ret, err := s.store.GetAccountRetention(ctx, a.ID)
		if err == nil && ret.GlobalDays.Valid {
			entry.GlobalDays = &ret.GlobalDays.Int64
		}
		entry.Labels, _ = s.store.GetLabelRetention(ctx, a.ID)
		entry.Exemptions, _ = s.store.GetLabelExemptions(ctx, a.ID)
		retentions = append(retentions, entry)
	}

	w.Header().Set("Content-Disposition", "attachment; filename=ollamail-config.json")
	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(configExport{ //nolint:musttag // sqlc-generated nested structs lack json tags by design
		Accounts:  safeAccounts,
		Prompts:   prompts,
		Settings:  settings,
		Retention: retentions,
	})
}

func (s *server) handleImportConfig(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	_ = r.ParseMultipartForm(10 << 20)
	file, _, err := r.FormFile("file")
	if err != nil {
		jsonError(w, "no file", 400)
		return
	}
	defer func() { _ = file.Close() }()

	var cfg configExport
	if err := json.NewDecoder(file).Decode(&cfg); err != nil { //nolint:musttag // sqlc-generated nested structs lack json tags by design
		jsonError(w, "invalid JSON", 400)
		return
	}

	imported := 0
	for _, p := range cfg.Prompts {
		exists, _ := s.store.PromptExistsGlobal(ctx, p.Name)
		if exists != 0 {
			continue
		}
		_, _ = s.store.CreatePrompt(ctx, db.CreatePromptParams{
			Name:           p.Name,
			Instructions:   p.Instructions,
			LabelName:      p.LabelName,
			ActionArchive:  p.ActionArchive,
			ActionSpam:     p.ActionSpam,
			ActionTrash:    p.ActionTrash,
			ActionMarkRead: p.ActionMarkRead,
			SortOrder:      p.SortOrder,
			StopProcessing: p.StopProcessing,
			AccountID:      p.AccountID,
		})
		imported++
	}
	for _, setting := range cfg.Settings {
		if setting.Key == "secret_key" {
			continue
		}
		_ = s.store.SeedSetting(setting.Key, setting.Value)
	}

	w.Header().Set("Content-Type", "application/json")
	_ = json.NewEncoder(w).Encode(map[string]any{"imported": imported})
}

func (s *server) handleDownloadLogs(w http.ResponseWriter, r *http.Request) {
	ctx := r.Context()
	q := r.URL.Query()
	start := q.Get("start")
	end := q.Get("end")

	var logs []db.Log
	if start != "" && end != "" {
		// Convert datetime-local (2006-01-02T15:04) to DB format
		start = strings.Replace(start, "T", " ", 1) + ":00"
		end = strings.Replace(end, "T", " ", 1) + ":00"
		logs, _ = s.store.GetLogsRange(ctx, db.GetLogsRangeParams{Timestamp: start, Timestamp_2: end})
	} else {
		logs, _ = s.store.GetLogs(ctx, 10000)
	}

	w.Header().Set("Content-Type", "text/csv")
	w.Header().Set("Content-Disposition", "attachment; filename=logs.csv")
	cw := csv.NewWriter(w)
	_ = cw.Write([]string{"timestamp", "level", "message"})
	for _, l := range logs {
		_ = cw.Write([]string{l.Timestamp, l.Level, l.Message})
	}
	cw.Flush()
}

func (s *server) handleGenerateStream(w http.ResponseWriter, r *http.Request) {
	description := r.URL.Query().Get("description")
	if description == "" {
		http.Error(w, "description required", 400)
		return
	}

	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-cache")
	w.Header().Set("X-Accel-Buffering", "no")

	flusher, ok := w.(http.Flusher)
	if !ok {
		http.Error(w, "streaming not supported", 500)
		return
	}

	ch := s.ollama.StreamGeneratePromptInstruction(r.Context(), description)
	for chunk := range ch {
		if chunk.Err != nil {
			break
		}
		b, _ := json.Marshal(map[string]string{"type": "content", "text": chunk.Text})
		_, _ = fmt.Fprintf(w, "data: %s\n\n", b)
		flusher.Flush()
	}
	_, _ = fmt.Fprintf(w, "data: {\"type\":\"done\"}\n\n")
	flusher.Flush()
}

// ============================================================
// Label pre-creation
// ============================================================

func (s *server) ensureLabelForAccounts(ctx context.Context, labelName string, accountID sql.NullInt64) {
	if labelName == "" {
		return
	}
	accounts, err := s.store.ListAccounts(ctx)
	if err != nil {
		return
	}
	oauthCfg, err := s.auth.ConfigFromFile()
	if err != nil {
		return
	}
	for _, account := range accounts {
		if accountID.Valid && accountID.Int64 != account.ID {
			continue
		}
		svc, err := gmail.NewService(ctx, account.CredentialsJson, oauthCfg, func(newCreds string) {
			_ = s.store.UpdateAccountCredentials(ctx, db.UpdateAccountCredentialsParams{
				CredentialsJson: newCreds, ID: account.ID,
			})
		})
		if err != nil {
			continue
		}
		_ = gmail.EnsureLabel(ctx, svc, labelName)
	}
}

// ============================================================
// Helpers
// ============================================================

func pathInt(r *http.Request, key string) int64 {
	v := r.PathValue(key)
	n, _ := strconv.ParseInt(v, 10, 64)
	return n
}

func boolToInt(b bool) int64 {
	if b {
		return 1
	}
	return 0
}

func generateToken(n int) string {
	b := make([]byte, n)
	_, _ = rand.Read(b)
	return hex.EncodeToString(b)
}

func jsonError(w http.ResponseWriter, msg string, code int) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]string{"error": msg})
}
