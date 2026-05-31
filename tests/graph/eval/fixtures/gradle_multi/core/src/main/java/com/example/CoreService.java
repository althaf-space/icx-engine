package com.example;

import org.springframework.stereotype.Service;

@Service
public class CoreService {
    public String greet(String name) {
        return "Hello, " + name;
    }
}
