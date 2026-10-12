from email.message import EmailMessage

from codex_mail_workbench.message import extract_text_body


def test_html_only_read_keeps_decisions_but_omits_layout_and_active_code():
    m = EmailMessage()
    m.set_content('<html><style>.padding{width:100%}</style><script>do_not_run()</script>'
                  '<body><p>Payment due <strong>20 October</strong>.</p>'
                  '<p>Balance: $123.45 &amp; account ending 1234.</p></body></html>', subtype="html")
    text = extract_text_body(m.as_bytes())
    assert "Payment due 20 October." in text and "$123.45 & account ending 1234." in text
    assert "padding" not in text and "do_not_run" not in text and "<html>" not in text


def test_plain_text_alternative_is_preserved_exactly():
    m = EmailMessage()
    m.set_content("Original plain\n\n  Indented line\n")
    m.add_alternative("<p>Different HTML</p>", subtype="html")
    assert extract_text_body(m.as_bytes()) == "Original plain\n\n  Indented line\n"
