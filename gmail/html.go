package gmail

import (
	"strings"
)

// extractText strips HTML tags from src and returns plain text.
// It skips content inside <script> and <style> elements.
func extractText(src string) string {
	srcLower := strings.ToLower(src)
	var sb strings.Builder
	inTag := false
	inScript := false
	inStyle := false
	i := 0

	for i < len(src) {
		if !inTag && !inScript && !inStyle {
			if src[i] == '<' {
				inTag = true
				// Peek at tag name using pre-lowercased copy
				peek := srcLower[i:min(i+8, len(src))]
				if strings.HasPrefix(peek, "<script") {
					inScript = true
				} else if strings.HasPrefix(peek, "<style") {
					inStyle = true
				}
				i++
				continue
			}
			sb.WriteByte(src[i])
			i++
			continue
		}

		if inScript {
			if idx := strings.Index(srcLower[i:], "</script>"); idx >= 0 {
				i += idx + len("</script>")
				inScript = false
				inTag = false
			} else {
				i = len(src)
			}
			continue
		}

		if inStyle {
			if idx := strings.Index(srcLower[i:], "</style>"); idx >= 0 {
				i += idx + len("</style>")
				inStyle = false
				inTag = false
			} else {
				i = len(src)
			}
			continue
		}

		if src[i] == '>' {
			inTag = false
			sb.WriteByte('\n')
		}
		i++
	}

	// Collapse whitespace lines
	lines := strings.Split(sb.String(), "\n")
	var out []string
	for _, l := range lines {
		t := strings.TrimSpace(l)
		if t != "" {
			out = append(out, t)
		}
	}
	return strings.Join(out, "\n")
}

// Truncate returns s truncated to maxChars bytes.
func Truncate(s string, maxChars int) string {
	if len(s) <= maxChars {
		return s
	}
	return s[:maxChars]
}
