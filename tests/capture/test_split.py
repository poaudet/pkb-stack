import random
from itertools import pairwise

from hub.capture.split import overlap_context, split_text

WORDS = ["alpha", "beta", "gamma", "delta", "Julie", "market", "Q3", "e.g.", "3.5", "ok"]


def random_text(rng, paragraphs):
    out = []
    for _ in range(paragraphs):
        if rng.random() < 0.25:
            out.append("Heading " + rng.choice(WORDS))
        sentences = [" ".join(rng.choice(WORDS) for _ in range(rng.randint(1, 40))) + "."
                     for _ in range(rng.randint(1, 8))]
        out.append(" ".join(sentences))
    return "\n\n".join(out)


def test_chunks_cover_text_without_gaps_and_respect_target():
    for seed in range(40):
        rng = random.Random(seed)
        text = random_text(rng, rng.randint(1, 30))
        target = rng.choice([50, 200, 600, 3000])
        chunks = split_text(text, target)
        assert chunks[0]["start_char"] == 0 and chunks[-1]["end_char"] == len(text)
        for before, after in pairwise(chunks):
            assert before["end_char"] == after["start_char"]
        assert "".join(text[c["start_char"]:c["end_char"]] for c in chunks) == text
        assert [c["idx"] for c in chunks] == list(range(len(chunks)))
        for chunk in chunks:
            body = text[chunk["start_char"]:chunk["end_char"]]
            if len(body) > target:
                from hub.capture.split import _boundaries
                inside = [b for b in _boundaries(text)
                          if chunk["start_char"] < b < chunk["end_char"]]
                assert not inside, "only an indivisible sentence may exceed the target"


def test_splitting_is_deterministic():
    text = random_text(random.Random(3), 20)
    assert split_text(text, 300) == split_text(text, 300)


def test_long_sentence_is_its_own_chunk_and_never_cut():
    sentence = "word " * 200 + "end."
    text = "Short one. " + sentence + " Tail."
    chunks = split_text(text, 100)
    bodies = [text[c["start_char"]:c["end_char"]] for c in chunks]
    assert any(body.strip() == sentence for body in bodies)


def test_overlap_is_previous_last_paragraph_tail():
    text = "A" * 50 + ".\n\n" + "para two. " * 30 + "\n\nlast paragraph here. " * 40
    chunks = split_text(text, 200)
    assert overlap_context(text, chunks, 0, 100) == ""
    context = overlap_context(text, chunks, 1, 100)
    assert 0 < len(context) <= 100
    assert context in text
