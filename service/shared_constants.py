"""Static business data shared by uc1_orchestration.py and
uc2_orchestration.py. n8n's two separate build_*_workflow.py scripts each
duplicated this map (deliberately, per their own convention of each
workflow-builder being self-contained) -- now that both live in one
codebase, one shared constant is the correct choice; it's purely static
string data, so consolidating it carries zero behavior-change risk."""

HSN_SAC_BY_CATEGORY = {
    "Furniture": "9403",
    "Software": "998313",
    "Services": "998311",
    "Food": "996331",
    "Appliances": "8516",
}
