package lab.searchops.api;

import jakarta.servlet.http.HttpServletRequest;
import java.time.OffsetDateTime;
import lab.searchops.domain.ApiModels.ErrorResponse;
import lab.searchops.service.StrategyService;
import org.slf4j.MDC;
import org.springframework.http.HttpStatus;
import org.springframework.http.ResponseEntity;
import org.springframework.web.bind.MethodArgumentNotValidException;
import org.springframework.web.bind.annotation.ExceptionHandler;
import org.springframework.web.bind.annotation.RestControllerAdvice;

@RestControllerAdvice
public class ApiExceptionHandler {
    @ExceptionHandler(StrategyService.NotFound.class)
    ResponseEntity<ErrorResponse> notFound(RuntimeException error) {
        return response(HttpStatus.NOT_FOUND, "not_found", error.getMessage());
    }

    @ExceptionHandler(StrategyService.Conflict.class)
    ResponseEntity<ErrorResponse> conflict(RuntimeException error) {
        return response(HttpStatus.CONFLICT, "conflict", error.getMessage());
    }

    @ExceptionHandler(StrategyService.Forbidden.class)
    ResponseEntity<ErrorResponse> forbidden(RuntimeException error) {
        return response(HttpStatus.FORBIDDEN, "forbidden", error.getMessage());
    }

    @ExceptionHandler({IllegalArgumentException.class, MethodArgumentNotValidException.class})
    ResponseEntity<ErrorResponse> invalid(Exception error) {
        return response(HttpStatus.BAD_REQUEST, "invalid_request", error.getMessage());
    }

    private ResponseEntity<ErrorResponse> response(HttpStatus status, String code, String message) {
        var requestId = MDC.get("requestId");
        return ResponseEntity.status(status).body(new ErrorResponse(code, message,
                requestId == null ? "unknown" : requestId, OffsetDateTime.now()));
    }
}

