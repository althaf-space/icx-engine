package com.example;

import org.springframework.web.bind.annotation.*;

@RestController
@RequestMapping("/api/v1/orders")
public class OrderController {
    @GetMapping
    public String list() { return "[]"; }

    @PostMapping
    public String create(@RequestBody String body) { return body; }

    @GetMapping("/{id}")
    public String get(@PathVariable Long id) { return id.toString(); }
}
