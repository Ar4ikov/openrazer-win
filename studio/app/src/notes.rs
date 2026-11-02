//! Release notes: finding the reader's language in a release's text, and
//! reading the little Markdown the changelog is written in.

use openrazer_studio_core::i18n::Lang;

/// A stretch of text in one style.
#[derive(Clone, Debug, Default, PartialEq)]
pub struct Span {
    pub text: String,
    pub bold: bool,
    pub italic: bool,
    pub code: bool,
    pub link: Option<String>,
}

#[derive(Clone, Debug, PartialEq)]
pub enum Block {
    /// *Added*, *Fixed*: what kind of change follows.
    Heading(String),
    /// A list item, `level` lists deep.
    Item { level: usize, spans: Vec<Span> },
    Text(Vec<Span>),
    Code(String),
}

fn section<'a>(body: &'a str, language: &str) -> Option<&'a str> {
    let open = format!("<!-- notes:{language} -->");
    let rest = &body[body.find(&open)? + open.len()..];
    Some(rest[..rest.find("<!-- /notes -->").unwrap_or(rest.len())].trim())
}

/// The part of a release's text for `language`: `tools/release_notes.py` marks
/// out each one. English stands in for a translation not written, and the
/// whole text for a release not made that way.
pub fn for_language(body: &str, language: Lang) -> &str {
    let wanted = if language == Lang::Ru { section(body, "ru") } else { None };
    wanted.or_else(|| section(body, "en")).unwrap_or(body.trim())
}

/// Text to the blocks it is made of. Anything not understood is shown as text;
/// HTML, which only GitHub's own page can make use of, is left out.
pub fn parse(text: &str) -> Vec<Block> {
    let mut blocks = Vec::new();
    let mut open: Option<(Option<usize>, String)> = None;
    let mut fence: Option<String> = None;

    fn close(open: &mut Option<(Option<usize>, String)>, blocks: &mut Vec<Block>) {
        match open.take() {
            Some((Some(level), text)) => blocks.push(Block::Item { level, spans: inline(&text) }),
            Some((None, text)) => blocks.push(Block::Text(inline(&text))),
            None => {}
        }
    }

    for line in text.lines() {
        let trimmed = line.trim();
        if let Some(code) = &mut fence {
            if trimmed.starts_with("```") {
                blocks.push(Block::Code(code.trim_end().to_string()));
                fence = None;
            } else {
                code.push_str(line);
                code.push('\n');
            }
            continue;
        }
        if trimmed.starts_with("```") {
            close(&mut open, &mut blocks);
            fence = Some(String::new());
        } else if trimmed.is_empty() {
            close(&mut open, &mut blocks);
        } else if trimmed.starts_with('<') || trimmed.starts_with("---") {
            continue;
        } else if trimmed.starts_with('#') {
            close(&mut open, &mut blocks);
            blocks.push(Block::Heading(trimmed.trim_start_matches('#').trim().to_string()));
        } else if let Some(item) = trimmed.strip_prefix("- ").or_else(|| trimmed.strip_prefix("* ")) {
            close(&mut open, &mut blocks);
            let indent = line.len() - line.trim_start().len();
            open = Some((Some((indent / 2).min(2)), item.to_string()));
        } else if let Some((_, text)) = &mut open {
            text.push(' ');
            text.push_str(trimmed);
        } else {
            open = Some((None, trimmed.to_string()));
        }
    }
    close(&mut open, &mut blocks);
    blocks
}

/// `[label](url)` starting at `at`: the label, the URL and where it ends.
fn link(chars: &[char], at: usize) -> Option<(String, String, usize)> {
    let close = at + chars[at..].iter().position(|&c| c == ']')?;
    if chars.get(close + 1) != Some(&'(') {
        return None;
    }
    let end = close + 1 + chars[close + 1..].iter().position(|&c| c == ')')?;
    Some((chars[at + 1..close].iter().collect(), chars[close + 2..end].iter().collect(), end + 1))
}

/// A line's emphasis, code and links.
pub fn inline(text: &str) -> Vec<Span> {
    let chars: Vec<char> = text.chars().collect();
    let mut spans = Vec::new();
    let mut plain = String::new();
    let (mut bold, mut italic) = (false, false);
    let mut i = 0;
    macro_rules! flush {
        () => {
            if !plain.is_empty() {
                spans.push(Span { text: std::mem::take(&mut plain), bold, italic, ..Default::default() });
            }
        };
    }
    while i < chars.len() {
        let next = chars.get(i + 1).copied();
        match chars[i] {
            '\\' if next.is_some_and(|c| c.is_ascii_punctuation()) => {
                plain.extend(next);
                i += 2;
            }
            '`' => match chars[i + 1..].iter().position(|&c| c == '`') {
                Some(length) => {
                    flush!();
                    spans.push(Span { text: chars[i + 1..i + 1 + length].iter().collect(), code: true, ..Default::default() });
                    i += length + 2;
                }
                None => {
                    plain.push('`');
                    i += 1;
                }
            },
            '*' if next == Some('*') => {
                flush!();
                bold = !bold;
                i += 2;
            }
            // Only hard against a word: `2 * 3` is not emphasis.
            '*' if (!italic && next.is_some_and(|c| !c.is_whitespace())) || (italic && i > 0 && !chars[i - 1].is_whitespace()) => {
                flush!();
                italic = !italic;
                i += 1;
            }
            '[' => match link(&chars, i) {
                Some((label, url, end)) => {
                    flush!();
                    // Anything else would be handed to Windows to open.
                    let link = url.starts_with("https://").then_some(url);
                    spans.push(Span { text: label, bold, italic, link, ..Default::default() });
                    i = end;
                }
                None => {
                    plain.push('[');
                    i += 1;
                }
            },
            c => {
                plain.push(c);
                i += 1;
            }
        }
    }
    flush!();
    spans
}

#[cfg(test)]
mod tests {
    use super::*;

    const BODY: &str = "<!-- notes:en -->\n### Added\n\n- One\n<!-- /notes -->\n\n<details>\n<summary>По-русски</summary>\n\n<!-- notes:ru -->\n### Добавлено\n\n- Раз\n<!-- /notes -->\n\n</details>\n\n**OpenRazer Studio** -- download it\n";

    #[test]
    fn the_notes_come_in_the_readers_language() {
        assert_eq!(for_language(BODY, Lang::Ru), "### Добавлено\n\n- Раз");
        assert_eq!(for_language(BODY, Lang::En), "### Added\n\n- One");
    }

    #[test]
    fn english_stands_in_for_a_missing_translation() {
        assert_eq!(for_language("<!-- notes:en -->\n- One\n<!-- /notes -->", Lang::Ru), "- One");
    }

    #[test]
    fn a_release_without_marks_shows_all_its_text() {
        assert_eq!(for_language("  just text\n", Lang::Ru), "just text");
    }

    fn plain(spans: &[Span]) -> String {
        spans.iter().map(|s| s.text.as_str()).collect()
    }

    #[test]
    fn a_list_item_runs_on_over_its_wrapped_lines() {
        let blocks = parse("### Added\n\n- **One**, on\r\n  two lines\n- Another:\n  - inside it\n\nAfter.\n");
        assert_eq!(blocks[0], Block::Heading("Added".into()));
        let Block::Item { level, spans } = &blocks[1] else { panic!("{:?}", blocks[1]) };
        assert_eq!((*level, plain(spans).as_str()), (0, "One, on two lines"));
        assert!(matches!(&blocks[2], Block::Item { level: 0, .. }));
        assert!(matches!(&blocks[3], Block::Item { level: 1, .. }));
        assert!(matches!(&blocks[4], Block::Text(spans) if plain(spans) == "After."));
    }

    #[test]
    fn html_and_rules_are_left_out_and_code_kept_as_it_is() {
        let blocks = parse("<details>\n---\n```\npip install *x*\n```\n");
        assert_eq!(blocks, [Block::Code("pip install *x*".into())]);
    }

    #[test]
    fn emphasis_nests_and_code_is_taken_literally() {
        let spans = inline("**A *Music* effect** and `a*b`");
        assert_eq!(spans.iter().map(|s| (s.text.as_str(), s.bold, s.italic, s.code)).collect::<Vec<_>>(), [
            ("A ", true, false, false),
            ("Music", true, true, false),
            (" effect", true, false, false),
            (" and ", false, false, false),
            ("a*b", false, false, true),
        ]);
    }

    #[test]
    fn a_star_between_spaces_is_only_a_star() {
        assert_eq!(inline("2 * 3 = 6"), [Span { text: "2 * 3 = 6".into(), ..Default::default() }]);
    }

    #[test]
    fn links_open_only_over_https() {
        let spans = inline("see [the docs](https://example.com/a) or [this](javascript:alert)");
        assert_eq!(spans[1].link.as_deref(), Some("https://example.com/a"));
        assert_eq!(spans[3].text, "this");
        assert_eq!(spans[3].link, None);
    }

    #[test]
    fn brackets_that_are_no_link_stay() {
        assert_eq!(plain(&inline("[1.5.3] of 2026")), "[1.5.3] of 2026");
    }
}
