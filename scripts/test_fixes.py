#!/usr/bin/env python3
from backend.intent.classifier import classify_intent
from backend.model.sql_generator import _matches_expected_table, generate_sql

sql = (
    "SELECT country, COUNT(*) FROM NETFLIX_TABLE WHERE type = 'Movie' "
    "GROUP BY country ORDER BY COUNT(*) DESC LIMIT 5;"
)
prompt = "What are the top 5 countries that have produced the most Movies on Netflix_table?"
intent = classify_intent(prompt)
params = intent["parameters"]
print("countries intent:", intent)
print("matches:", _matches_expected_table(sql, params.get("action_hint", "auto"), params.get("expected_table", "")))

create_prompt = (
    "Create a new table netflix_reviews_test with columns for review_id "
    "as an integer, title as a string"
)
create_intent = classify_intent(create_prompt)
print("create expected_table:", create_intent["parameters"].get("expected_table"))

try:
    out, model = generate_sql(prompt, intent["intent"], params)
    print("generate_sql OK:", model, out[:100])
except Exception as exc:
    print("generate_sql FAIL:", exc)
