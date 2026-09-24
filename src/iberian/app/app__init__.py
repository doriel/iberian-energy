"""What the deployed application needs, and nothing the pipeline needs.

Kept apart from the rest of the package because the web service installs a much
shorter requirements file than the pipeline does. An import that reaches from
here into the analysis modules would drag pandas into a deploy that has no use
for it, and the first sign would be a cold start getting slower.
"""