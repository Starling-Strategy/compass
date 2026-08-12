<guidance topic="peer-policy-comparison">
<title>Peer policy comparison</title>
<scope>Applies when the user names one anchor district and asks for peer, similar, or comparable districts alongside a governed policy topic.</scope>

- Use `operation="peer_comparison"` for anchor-plus-peer requests that include a policy topic.
- Place only the anchor district in `selection.districts`; omit labels like "peer districts" or "comparable districts" from the district list.
- Put the user's policy phrase in `metrics`; the CatalogResolver owns final metric, bundle, or clarification decisions.
- The peer-vs-similarity routing rule (similarity = who the peers are; `peer_comparison` = peers plus a policy metric) is canonical in `similarity-discovery.md`; route to `operation="similarity"` only when no policy metric, topic, compensation field, benefit, leave/observation policy, premium, stipend, or evaluation policy is mentioned.

<examples>
<example>
User: "I work in Denver Public Schools. Who are our peer districts and how do our sick leave policies compare?"
Plan shape: operation="peer_comparison", selection.districts=["Denver Public Schools"], metrics=[{name="sick leave policy"}].
</example>
<example>
User: "I am from Philadelphia, show me ten similar districts and their benefits information."
Plan shape: operation="peer_comparison", selection.districts=["Philadelphia"], metrics=[{name="benefits information"}].
</example>
</examples>
</guidance>
