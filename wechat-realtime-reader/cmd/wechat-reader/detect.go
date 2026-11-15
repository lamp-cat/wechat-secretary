package main

// detect: locate the local WeChat 4.x account directories (read-only) and,
// optionally, write source-config.json for the chosen account.
// Never reads key material and never touches WeChat itself.

import (
	"encoding/json"
	"errors"
	"flag"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"github.com/missuo/wechat-realtime-reader/internal/config"
)

type account struct {
	Wxid       string    `json:"wxid"`
	DBBasePath string    `json:"db_base_path"`
	HasMessage bool      `json:"has_message_db"`
	LastActive time.Time `json:"last_active"`
}

// wechatFilesRoots lists candidate xwechat_files roots, most likely first.
// WECHAT_FILES_ROOT overrides discovery (one or more paths, ':'-separated).
func wechatFilesRoots() []string {
	if v := os.Getenv("WECHAT_FILES_ROOT"); v != "" {
		return filepath.SplitList(v)
	}
	home, _ := os.UserHomeDir()
	return []string{
		filepath.Join(home, "Library/Containers/com.tencent.xinWeChat/Data/Documents/xwechat_files"),
		filepath.Join(home, "Library/Containers/com.tencent.xinWeChat/Data/Library/Application Support/com.tencent.xinWeChat/xwechat_files"),
		filepath.Join(home, "Documents/xwechat_files"),
	}
}

// newestMtime returns the latest mtime among the db files that change while
// an account is logged in (message/session/contact dbs and their WALs).
func newestMtime(dbs string) (time.Time, bool) {
	var newest time.Time
	hasMsg := false
	for _, sub := range []string{"message", "session", "contact"} {
		ents, err := os.ReadDir(filepath.Join(dbs, sub))
		if err != nil {
			continue
		}
		for _, e := range ents {
			n := e.Name()
			if !strings.Contains(n, ".db") {
				continue
			}
			if sub == "message" && strings.HasPrefix(n, "message_0.db") {
				hasMsg = true
			}
			if fi, err := e.Info(); err == nil && fi.ModTime().After(newest) {
				newest = fi.ModTime()
			}
		}
	}
	return newest, hasMsg
}

func findAccounts() ([]account, error) {
	var out []account
	var permErr error
	for _, root := range wechatFilesRoots() {
		ents, err := os.ReadDir(root)
		if err != nil {
			if errors.Is(err, fs.ErrPermission) {
				permErr = err
			}
			continue
		}
		for _, e := range ents {
			if !e.IsDir() || e.Name() == "all_users" || e.Name() == "Backup" || strings.HasPrefix(e.Name(), ".") {
				continue
			}
			dbs := filepath.Join(root, e.Name(), "db_storage")
			if st, err := os.Stat(dbs); err != nil || !st.IsDir() {
				if errors.Is(err, fs.ErrPermission) {
					permErr = err
				}
				continue
			}
			t, has := newestMtime(dbs)
			out = append(out, account{Wxid: e.Name(), DBBasePath: dbs, HasMessage: has, LastActive: t})
		}
	}
	sort.SliceStable(out, func(i, j int) bool {
		if out[i].HasMessage != out[j].HasMessage {
			return out[i].HasMessage
		}
		return out[i].LastActive.After(out[j].LastActive)
	})
	if len(out) == 0 && permErr != nil {
		return nil, fmt.Errorf("permission denied reading the WeChat data folder (grant access in System Settings → Privacy & Security)")
	}
	return out, nil
}

func cmdDetect(args []string) error {
	fs := flag.NewFlagSet("detect", flag.ExitOnError)
	write := fs.Bool("write", false, "write source-config.json for the chosen account if it does not exist yet")
	force := fs.Bool("force", false, "with -write: replace an existing source-config.json (a backup is kept)")
	pick := fs.String("wxid", "", "account directory to use (default: the most recently active one)")
	asJSON := fs.Bool("json", false, "machine-readable output")
	fs.Parse(args)

	accts, err := findAccounts()
	if err != nil {
		return err
	}
	res := map[string]any{"accounts": accts, "roots": wechatFilesRoots()}
	var chosen *account
	for i := range accts {
		if *pick == "" || accts[i].Wxid == *pick {
			chosen = &accts[i]
			break
		}
	}
	if chosen != nil {
		res["chosen"] = chosen.Wxid
	}
	if *write {
		if chosen == nil {
			return errors.New("no WeChat 4.x account directory found (log in to WeChat once on this Mac first)")
		}
		written, err := writeSourceConfig(*chosen, *force)
		if err != nil {
			return err
		}
		res["written"] = written
	}
	if *asJSON {
		enc := json.NewEncoder(os.Stdout)
		enc.SetIndent("", "  ")
		return enc.Encode(res)
	}
	if len(accts) == 0 {
		fmt.Println("no WeChat 4.x account directory found")
		return nil
	}
	for _, a := range accts {
		mark := " "
		if chosen != nil && a.Wxid == chosen.Wxid {
			mark = "*"
		}
		fmt.Printf("%s %-40s last active %s  message db: %v\n", mark, a.Wxid, a.LastActive.Format("2006-01-02 15:04"), a.HasMessage)
	}
	if w, ok := res["written"].(bool); ok {
		fmt.Printf("source-config.json written: %v\n", w)
	}
	return nil
}

// writeSourceConfig creates ~/.config/wechat-realtime-reader/source-config.json
// (dir 0700, file 0600). Existing files are kept unless force is set.
func writeSourceConfig(a account, force bool) (bool, error) {
	dir := config.DefaultDir()
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return false, err
	}
	_ = os.Chmod(dir, 0o700)
	p := filepath.Join(dir, "source-config.json")
	if _, err := os.Stat(p); err == nil {
		if !force {
			return false, nil
		}
		b, _ := os.ReadFile(p)
		_ = os.WriteFile(p+".bak-"+time.Now().Format("20060102-150405"), b, 0o600)
	}
	home, _ := os.UserHomeDir()
	src := config.Source{
		Wxid:       a.Wxid,
		DBBasePath: a.DBBasePath,
		VaultDir:   filepath.Join(home, ".local", "share", "wechat-realtime-reader"),
	}
	b, _ := json.MarshalIndent(src, "", "  ")
	tmp, err := os.CreateTemp(dir, ".source-config-*")
	if err != nil {
		return false, err
	}
	defer os.Remove(tmp.Name())
	if err := tmp.Chmod(0o600); err != nil {
		tmp.Close()
		return false, err
	}
	if _, err := tmp.Write(append(b, '\n')); err != nil {
		tmp.Close()
		return false, err
	}
	tmp.Close()
	return true, os.Rename(tmp.Name(), p)
}
