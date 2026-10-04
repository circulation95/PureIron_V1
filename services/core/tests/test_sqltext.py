import pytest

from pureiron_core.platform.sqltext import SqlSyntaxError, split_statements


def test_splits_on_top_level_semicolons_only() -> None:
    script = """
    -- leading comment; not a boundary
    CREATE TABLE a (x text DEFAULT 'semi;colon');
    /* block; /* nested; */ still comment */
    SELECT $$ body; with ; semis $$, $tag$ more; $tag$;
    INSERT INTO "odd;name" VALUES (E'it\\'s; fine');
    """
    statements = split_statements(script)
    assert [s.code.split()[0] for s in statements] == ["CREATE", "SELECT", "INSERT"]
    assert statements[0].sql.endswith("DEFAULT 'semi;colon')")
    assert "$$ body; with ; semis $$" in statements[1].sql


def test_code_view_blanks_literals_and_comments() -> None:
    (statement,) = split_statements("select 'DROP TABLE x' -- drop me\n, \"TRUNCATE\" from t")
    assert statement.code == "SELECT '' , \"\" FROM T"


def test_comment_only_script_has_no_statements() -> None:
    assert split_statements("-- nothing\n/* here */\n;;") == []


@pytest.mark.parametrize("script", ["select 'open", "select $x$ open", "/* open", 'select "open'])
def test_unterminated_input_is_rejected(script: str) -> None:
    with pytest.raises(SqlSyntaxError):
        split_statements(script)


def test_doubled_quotes_stay_inside_literal() -> None:
    (statement,) = split_statements("select 'a''b;c'")
    assert statement.sql == "select 'a''b;c'"
