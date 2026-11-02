"""Plot the stored commercial perturbation results."""

import json
from pathlib import Path

import matplotlib.pyplot as plt

here = Path(__file__).resolve().parent
rows = json.loads((here / 'uncertainty.json').read_text())['cases']
offsets = {'tq': (8, 0), 'mosrac': (-53, 7), 'kollmorgen': (8, 0),
           'cubemars': (8, 0), 'segmented': (8, 0), 'ring': (8, 0)}
fig, ax = plt.subplots(figsize=(7.2, 4.8), layout='constrained')
for row in rows:
    nominal = row['nominal_Nm']
    ax.errorbar(
        row['envelope_cm3'], nominal,
        yerr=[[nominal - row['min_Nm']], [row['max_Nm'] - nominal]],
        fmt='o', color='#235c84', capsize=4, markersize=5, linewidth=1.5,
        label='Nominal and sampled range' if row is rows[0] else None,
    )
    ax.plot(
        row['envelope_cm3'], row['catalog_Nm'], 'D', color='#bf641f',
        markerfacecolor='none', markersize=7, markeredgewidth=1.3, zorder=3,
        label='Catalog equivalent' if row is rows[0] else None,
    )
    ax.annotate(row['label'], (row['envelope_cm3'], nominal),
                xytext=offsets[row['id']], textcoords='offset points',
                fontsize=9, va='center')
ax.set(xlabel='Envelope volume (cm³)', ylabel='Torque at 5.5 W copper loss (N·m)',
       xlim=(20, 76), ylim=(0, .45))
ax.spines[['top', 'right']].set_visible(False)
ax.grid(axis='y', color='#e4e8eb', linewidth=.7)
ax.set_axisbelow(True)
ax.legend(frameon=False, fontsize=9, loc='upper right')
fig.savefig(here / 'torque.png', dpi=220)
