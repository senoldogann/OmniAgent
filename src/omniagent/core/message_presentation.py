"""Native Messages accepts plain text; adapt presentation without rewriting content."""
from __future__ import annotations

import re


def imessage_plain_text(text: str) -> str:
    """Remove common Markdown syntax, protecting fenced/inline code verbatim.

    Links retain their exact destination. No Unicode styling, truncation, model
    rewrite or whitespace normalization is performed on code contents.
    """
    protected: list[str] = []
    # Select a token absent from the input; substitutions cannot collide with prose.
    token = '\x00OMNI_CODE'
    while token in text:
        token += '_'
    def protect(value: str) -> str:
        protected.append(value)
        return token + str(len(protected) - 1) + '\x00'
    # Only closed fences are formatting. Unclosed code is preserved literally.
    def fence(match: re.Match) -> str:
        return protect(match['body'])
    text = re.sub(r'(?m)^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*\n(?P<body>[\s\S]*?)^[ \t]*(?P=fence)[ \t]*(?:\n|$)', fence, text)
    text = re.sub(r'(?m)^[ \t]*(?:`{3,}|~{3,})[^\n]*\n[\s\S]*$', lambda m: protect(m[0]), text)
    text = re.sub(r'(?P<t>`+)(?P<body>[^`\n]+)(?P=t)', lambda m: protect(m['body']), text)
    text = re.sub(r'!?\[([^\]\n]+)\]\((https?://[^\s]+)\)', lambda m: m[1] + ' (' + protect(m[2]) + ')', text)
    # URLs are literal destinations, even when they contain Markdown delimiters.
    def url(match: re.Match) -> str:
        value = match[0]
        # A surrounding bold delimiter is markup; delimiters inside a URL are data.
        if text[max(0, match.start() - 2):match.start()] == '**' and value.endswith('**'):
            return protect(value[:-2]) + '**'
        return protect(value)
    text = re.sub(r'https?://[^\s<>]+', url, text)
    text = re.sub(r'(?m)^([ \t]{0,3})#{1,6}[ \t]+(.+?)[ \t]*#*[ \t]*$', r'\1\2', text)
    text = re.sub(r'(?m)^([ \t]*)[-*+][ \t]+', r'\1• ', text)
    # Require paired delimiters; underscores inside filenames stay untouched.
    text = re.sub(r'\*\*(?=\S)(.+?)(?<=\S)\*\*', lambda m: m[1], text)
    text = re.sub(r'(?<!\w)\*(?=\S)([^*\n]+)(?<=\S)\*(?!\w)', lambda m: m[1], text)
    for index, value in enumerate(protected):
        text = text.replace(token + str(index) + '\x00', value)
    return text
