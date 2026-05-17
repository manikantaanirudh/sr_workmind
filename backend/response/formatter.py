from typing import Any


def build_user_message(intent: str, rows: list[list[Any]]) -> str:
    if not rows:
        return "Query executed successfully, but no rows were returned."

    if intent == "summarize_table":
        return "Table summary generated successfully."

    if intent == "top_customers":
        names = ", ".join(str(r[0]) for r in rows[:5])
        return f"Top customers by revenue are: {names}."

    if intent == "total_sales_by_region":
        return "Total sales by region retrieved successfully."

    if intent == "last_orders":
        return "Latest orders retrieved successfully."

    # --- Salesforce-specific messages ---
    if intent == "sf_query":
        count = len(rows)
        return f"Salesforce query returned {count} record{'s' if count != 1 else ''}."

    if intent == "sf_search":
        count = len(rows)
        return f"Salesforce search returned {count} result{'s' if count != 1 else ''}."

    if intent == "sf_record_created":
        return "Salesforce record created successfully."

    if intent == "sf_record_updated":
        return "Salesforce record updated successfully."

    if intent == "sf_record_deleted":
        return "Salesforce record deleted successfully."

    if intent == "sf_schema":
        return "Salesforce object schema retrieved."

    # --- Docusign-specific messages ---
    if intent == "ds_query_agreements":
        count = len(rows)
        return f"Docusign agreement query returned {count} row{'s' if count != 1 else ''}."

    if intent == "ds_agreement_details":
        return "Docusign agreement details retrieved."

    if intent == "ds_workflow_requirements":
        return "Docusign workflow trigger requirements retrieved."

    if intent == "ds_trigger_workflow":
        if rows and str(rows[0][0]).lower().startswith("confirmation required"):
            return "Docusign workflow trigger is ready, but needs explicit confirmation before execution."
        return "Docusign workflow triggered successfully."

    return "Query executed successfully."

