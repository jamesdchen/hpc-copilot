# %%
# hpc-audit-section: setup
# Toy audit template shipped BY the toy-widgets pack (S4). Percent-format .py with
# opaque section slugs — core hashes it and tracks section drift, never runs it.
widget_seed = 0
rmse_threshold = 0.01

# %%
# hpc-audit-section: load-widgets
widget_count = 3

# %%
# hpc-audit-section: compute-rmse
rmse = 0.0

# %%
# hpc-audit-section: report
print(f"widgets={widget_count} rmse={rmse} threshold={rmse_threshold} seed={widget_seed}")
