package com.example;

import org.springframework.stereotype.Service;

@Service
public class OrderServiceImpl implements OrderService {
    @Override
    public String createOrder(String item) {
        return "created:" + item;
    }

    @Override
    public String getOrder(Long id) {
        return "order:" + id;
    }
}
