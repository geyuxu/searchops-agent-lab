package lab.searchops.service;

import tools.jackson.databind.JsonNode;
import tools.jackson.databind.ObjectMapper;
import java.io.BufferedReader;
import java.io.IOException;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.util.ArrayList;
import java.util.HexFormat;
import java.util.List;
import java.util.Map;
import lab.searchops.config.SearchProperties;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.core.io.ClassPathResource;
import org.springframework.http.MediaType;
import org.springframework.stereotype.Component;
import org.springframework.web.client.RestClient;
import org.springframework.web.client.RestClientResponseException;

@Component
public class ElasticsearchGateway {
    private static final MediaType NDJSON = MediaType.parseMediaType("application/x-ndjson");

    private final RestClient client;
    private final ObjectMapper mapper;
    private final SearchProperties properties;

    public ElasticsearchGateway(@Qualifier("elasticsearchRestClient") RestClient client,
            ObjectMapper mapper, SearchProperties properties) {
        this.client = client;
        this.mapper = mapper;
        this.properties = properties;
    }

    public JsonNode search(Map<String, Object> body) {
        return client.post().uri("/{index}/_search", properties.indexAlias())
                .contentType(MediaType.APPLICATION_JSON).body(body).retrieve().body(JsonNode.class);
    }

    public JsonNode getProduct(String productId) {
        try {
            return client.get().uri("/{index}/_doc/{id}", properties.indexAlias(), productId)
                    .retrieve().body(JsonNode.class);
        } catch (RestClientResponseException exception) {
            if (exception.getStatusCode().value() == 404) {
                return null;
            }
            throw exception;
        }
    }

    public JsonNode explain(String productId, Map<String, Object> query) {
        return client.post().uri("/{index}/_explain/{id}", properties.indexAlias(), productId)
                .contentType(MediaType.APPLICATION_JSON).body(Map.of("query", query))
                .retrieve().body(JsonNode.class);
    }

    public Map<String, Object> rebuild(String requestedPath) {
        var source = allowedPath(requestedPath);
        if (!Files.isRegularFile(source)) {
            throw new IllegalArgumentException("Processed products file not found: " + source);
        }
        putTemplate();
        var index = "products-v" + sha256(source).substring(0, 12);
        var expected = countLines(source);
        if (indexExists(index) && count(index) != expected) {
            client.delete().uri("/{index}", index).retrieve().toBodilessEntity();
        }
        if (!indexExists(index)) {
            client.put().uri("/{index}", index).contentType(MediaType.APPLICATION_JSON)
                    .body(Map.of()).retrieve().toBodilessEntity();
            importProducts(source, index);
            client.post().uri("/{index}/_refresh", index).retrieve().toBodilessEntity();
        }
        var actual = count(index);
        if (actual != expected) {
            throw new IllegalStateException("Index count mismatch: expected " + expected + ", got " + actual);
        }
        moveAlias(index);
        return Map.of("index", index, "alias", properties.indexAlias(), "documents", actual,
                "source", source.toString(), "idempotent", true);
    }

    private Path allowedPath(String requestedPath) {
        var base = Path.of(properties.processedDataPath()).toAbsolutePath().normalize();
        var resolved = requestedPath == null || requestedPath.isBlank()
                ? base.resolve("products.jsonl") : Path.of(requestedPath).toAbsolutePath().normalize();
        if (!resolved.startsWith(base)) {
            throw new IllegalArgumentException("Index source must stay under " + base);
        }
        return resolved;
    }

    private void putTemplate() {
        try (var input = new ClassPathResource("elasticsearch/product-index-template.json").getInputStream()) {
            var template = mapper.readTree(input);
            client.put().uri("/_index_template/searchops-products")
                    .contentType(MediaType.APPLICATION_JSON).body(template).retrieve().toBodilessEntity();
        } catch (IOException exception) {
            throw new IllegalStateException("Unable to load index template", exception);
        }
    }

    private boolean indexExists(String index) {
        try {
            client.head().uri("/{index}", index).retrieve().toBodilessEntity();
            return true;
        } catch (RestClientResponseException exception) {
            if (exception.getStatusCode().value() == 404) {
                return false;
            }
            throw exception;
        }
    }

    private void importProducts(Path source, String index) {
        try (BufferedReader reader = Files.newBufferedReader(source, StandardCharsets.UTF_8)) {
            var batch = new ArrayList<String>(1000);
            String line;
            while ((line = reader.readLine()) != null) {
                if (line.isBlank()) continue;
                batch.add(line);
                if (batch.size() >= 500) {
                    sendBulk(batch, index);
                    batch.clear();
                }
            }
            if (!batch.isEmpty()) sendBulk(batch, index);
        } catch (IOException exception) {
            throw new IllegalStateException("Unable to read processed products", exception);
        }
    }

    private void sendBulk(List<String> products, String index) {
        var ndjson = new StringBuilder(products.size() * 1000);
        for (var product : products) {
            var id = mapper.readTree(product).path("product_id").asText();
            if (id.isBlank()) throw new IllegalArgumentException("Product has no product_id");
            ndjson.append(mapper.writeValueAsString(
                    Map.of("index", Map.of("_index", index, "_id", id)))).append('\n');
            ndjson.append(product).append('\n');
        }
        var response = client.post().uri("/_bulk").contentType(NDJSON)
                .body(ndjson.toString().getBytes(StandardCharsets.UTF_8))
                .retrieve().body(JsonNode.class);
        if (response != null && response.path("errors").asBoolean()) {
            var failures = new ArrayList<String>();
            response.path("items").forEach(item -> {
                var error = item.path("index").path("error");
                if (!error.isMissingNode()) failures.add(error.toString());
            });
            throw new IllegalStateException("Elasticsearch bulk import failed: "
                    + failures.stream().limit(3).toList());
        }
    }

    private long count(String index) {
        var response = client.get().uri("/{index}/_count", index).retrieve().body(JsonNode.class);
        return response == null ? 0 : response.path("count").asLong();
    }

    private void moveAlias(String index) {
        var actions = List.of(
                Map.of("remove", Map.of("index", "products-v*", "alias", properties.indexAlias(),
                        "must_exist", false)),
                Map.of("add", Map.of("index", index, "alias", properties.indexAlias())));
        client.post().uri("/_aliases").contentType(MediaType.APPLICATION_JSON)
                .body(Map.of("actions", actions)).retrieve().toBodilessEntity();
    }

    private long countLines(Path source) {
        try (var lines = Files.lines(source, StandardCharsets.UTF_8)) {
            return lines.filter(line -> !line.isBlank()).count();
        } catch (IOException exception) {
            throw new IllegalStateException("Unable to count products", exception);
        }
    }

    private String sha256(Path source) {
        try (var input = Files.newInputStream(source)) {
            var digest = MessageDigest.getInstance("SHA-256");
            var buffer = new byte[1024 * 1024];
            int read;
            while ((read = input.read(buffer)) >= 0) digest.update(buffer, 0, read);
            return HexFormat.of().formatHex(digest.digest());
        } catch (Exception exception) {
            throw new IllegalStateException("Unable to hash processed products", exception);
        }
    }
}
