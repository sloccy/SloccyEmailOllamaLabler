package poller

import (
	"context"
	"log/slog"
	"strconv"
	"sync"
	"time"

	"github.com/sloccy/ollamail/db"
	"github.com/sloccy/ollamail/gmail"
	"github.com/sloccy/ollamail/llm"
	"github.com/sloccy/ollamail/processor"
	"github.com/sloccy/ollamail/retention"
)

const cleanupInterval = time.Hour

// Poller runs background email scans on a configurable interval.
type Poller struct {
	store        *db.Store
	ollamaClient *llm.Client
	gmailAuth    *gmail.Auth
	cfg          *Config

	mu          sync.RWMutex
	interval    time.Duration
	lastRun     time.Time
	nextRun     time.Time
	lastCleanup time.Time

	scanMu  sync.Mutex    // non-blocking try-lock for scan exclusion
	resetCh chan struct{} // signals loop to reset the timer after interval change

	cancel context.CancelFunc
}

// Config holds the runtime configuration needed by the poller.
type Config struct {
	LookbackHours  int
	MaxResults     int64
	BodyTruncation int
	LogRetention   int
	DebugLogging   bool
}

// Status is returned by GetStatus.
type Status struct {
	Running bool
	LastRun string
	NextRun string
}

func New(store *db.Store, ollamaClient *llm.Client, auth *gmail.Auth, cfg *Config) *Poller {
	return &Poller{
		store:        store,
		ollamaClient: ollamaClient,
		gmailAuth:    auth,
		cfg:          cfg,
		resetCh:      make(chan struct{}, 1),
	}
}

// Start begins the polling loop. Reads interval from DB settings.
func (p *Poller) Start() {
	ctx := context.Background()
	val, err := p.store.GetSetting(ctx, "poll_interval")
	if err == nil {
		if n, err := strconv.Atoi(val); err == nil && n > 0 {
			p.mu.Lock()
			p.interval = time.Duration(n) * time.Second
			p.mu.Unlock()
		}
	}
	if p.interval == 0 {
		p.interval = 5 * time.Minute
	}

	p.mu.Lock()
	p.nextRun = time.Now()
	p.mu.Unlock()

	loopCtx, cancel := context.WithCancel(context.Background())
	p.cancel = cancel
	go p.loop(loopCtx)
}

// Stop signals the poller loop to exit.
func (p *Poller) Stop() {
	if p.cancel != nil {
		p.cancel()
	}
}

// RunNow triggers a scan and blocks until it completes.
func (p *Poller) RunNow() {
	p.runScan()
}

// UpdateInterval changes the polling interval and reschedules the next run.
func (p *Poller) UpdateInterval(seconds int) {
	p.mu.Lock()
	p.interval = time.Duration(seconds) * time.Second
	p.nextRun = p.lastRun.Add(p.interval)
	p.mu.Unlock()
	// Signal loop to reset the timer so the new interval takes effect immediately.
	select {
	case p.resetCh <- struct{}{}:
	default:
	}
}

// GetStatus returns current poller state.
func (p *Poller) GetStatus() Status {
	p.mu.RLock()
	defer p.mu.RUnlock()
	var lastRun, nextRun string
	if !p.lastRun.IsZero() {
		lastRun = p.lastRun.UTC().Format("2006-01-02 15:04:05")
	}
	if !p.nextRun.IsZero() {
		nextRun = p.nextRun.UTC().Format("2006-01-02 15:04:05")
	}
	running := !p.scanMu.TryLock()
	if !running {
		p.scanMu.Unlock()
	}
	return Status{
		Running: running,
		LastRun: lastRun,
		NextRun: nextRun,
	}
}

func (p *Poller) loop(ctx context.Context) {
	p.mu.RLock()
	d := time.Until(p.nextRun)
	p.mu.RUnlock()
	if d < 0 {
		d = 0
	}
	timer := time.NewTimer(d)
	defer timer.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-p.resetCh:
			if !timer.Stop() {
				select {
				case <-timer.C:
				default:
				}
			}
			p.mu.RLock()
			d = time.Until(p.nextRun)
			p.mu.RUnlock()
			if d < 0 {
				d = 0
			}
			timer.Reset(d)
		case <-timer.C:
			p.runScan()
			p.mu.Lock()
			d = p.interval
			p.nextRun = time.Now().Add(p.interval)
			p.mu.Unlock()
			timer.Reset(d)
		}
	}
}

func (p *Poller) runScan() {
	if !p.scanMu.TryLock() {
		return // scan already running
	}
	defer p.scanMu.Unlock()

	now := time.Now()
	p.mu.Lock()
	p.lastRun = now
	doCleanup := now.Sub(p.lastCleanup) >= cleanupInterval
	if doCleanup {
		p.lastCleanup = now
	}
	p.mu.Unlock()

	ctx := context.Background()

	if doCleanup {
		_ = p.store.TrimLogs(ctx, p.cfg.LogRetention)
		_ = p.store.TrimProcessedEmails(ctx, p.cfg.LookbackHours)
		_ = p.store.TrimHistory(ctx, p.cfg.LogRetention)
	}

	accounts, err := p.store.ListAccounts(ctx)
	if err != nil {
		slog.Error("list accounts", "err", err)
		return
	}

	prompts, err := p.store.ListActivePrompts(ctx)
	if err != nil {
		slog.Error("list prompts", "err", err)
		return
	}

	procCfg := processor.ProcessConfig{
		LookbackHours:  p.cfg.LookbackHours,
		MaxResults:     p.cfg.MaxResults,
		BodyTruncation: p.cfg.BodyTruncation,
		DebugLogging:   p.cfg.DebugLogging,
	}

	for _, account := range accounts {
		if account.Active == 0 {
			continue
		}
		wrapper, err := processor.ProcessAccount(ctx, p.store, p.ollamaClient, p.gmailAuth, account, prompts, procCfg)
		if err != nil {
			slog.Error("process account", "email", account.Email, "err", err)
			p.store.Log("ERROR", "Scan failed for "+account.Email+": "+err.Error())
			continue
		}
		if wrapper != nil {
			retention.Cleanup(ctx, p.store, wrapper.Svc, account.ID)
		}
	}
}
